"""
Intent Parser Service — orchestrates the full NL → validated config pipeline.

Pipeline steps:
1. Pre-process input (sanitize, extract context hints)
2. Call LLM Gateway → get ParsedConfiguration
3. Map ParsedConfiguration → concrete Sovereign schema
4. Deterministic validation against schema + business rules
5. Blast radius simulation
6. Return validated config or rejection with explanation
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog
from ulid import ULID

from broker.schemas.intent import (
    IntentAction,
    IntentResponse,
    ParsedConfiguration,
)
from broker.schemas.sovereign import (
    ClusterConfig,
    LoadBalancingPolicy,
    RateLimitConfig,
    RouteConfig,
    WeightedCluster,
)
from broker.services.llm_gateway import LLMGateway
from broker.services.safety import SafetyService

logger = structlog.get_logger()


class IntentParserService:
    """Orchestrates the AI intent parsing pipeline with safety guardrails."""

    def __init__(self, llm_gateway: LLMGateway, safety_service: SafetyService) -> None:
        self._llm = llm_gateway
        self._safety = safety_service

    async def parse_and_validate(
        self,
        natural_language: str,
        context: dict[str, Any] | None = None,
    ) -> IntentResponse:
        """Execute the full NL → validated configuration pipeline.

        Args:
            natural_language: Developer's plain English request.
            context: Optional context metadata.

        Returns:
            IntentResponse with parsed config, validation, and blast radius.
        """
        request_id = str(ULID())

        await logger.ainfo(
            "Starting intent parsing pipeline",
            request_id=request_id,
            input_length=len(natural_language),
        )

        # Step 1: Sanitize input
        sanitized_input = self._sanitize_input(natural_language)

        # Step 2: LLM translation
        parsed_config = await self._llm.parse_intent(sanitized_input, context)
        confidence = await self._llm.get_confidence_score()

        await logger.ainfo(
            "LLM translation completed",
            request_id=request_id,
            action=parsed_config.action,
            target_service=parsed_config.target_service,
            confidence=confidence,
        )

        # Step 3: Map to concrete Sovereign schema (validate structure)
        sovereign_config = self._map_to_sovereign_schema(parsed_config)

        # Step 4: Deterministic validation
        validation = await self._safety.validate_config(parsed_config, sovereign_config)

        # Step 5: Blast radius simulation
        blast_radius = await self._safety.simulate_blast_radius(parsed_config)

        # Collect warnings
        warnings: list[str] = []
        if confidence < 0.8:
            warnings.append(
                f"Low confidence score ({confidence:.2f}). "
                "Review the parsed configuration carefully before applying."
            )
        if blast_radius.risk_score > 0.5:
            warnings.append(
                f"Elevated blast radius risk ({blast_radius.risk_score:.2f}). "
                f"Affected services: {', '.join(blast_radius.affected_services)}"
            )
        warnings.extend(validation.warnings)

        return IntentResponse(
            request_id=request_id,
            original_input=natural_language,
            parsed_configuration=parsed_config,
            validation=validation,
            blast_radius=blast_radius,
            confidence_score=confidence,
            warnings=warnings,
            created_at=datetime.now(UTC),
        )

    def _sanitize_input(self, text: str) -> str:
        """Basic input sanitization."""
        # Strip excessive whitespace
        sanitized = " ".join(text.split())
        # Truncate to reasonable length
        return sanitized[:2000]

    def _map_to_sovereign_schema(
        self,
        config: ParsedConfiguration,
    ) -> RouteConfig | ClusterConfig | RateLimitConfig | None:
        """Map a ParsedConfiguration to the appropriate Sovereign schema.

        This validates that the AI output can be translated into a real
        Envoy configuration structure. Returns None for actions that don't
        map directly to a single schema (e.g., scale_service).
        """
        params = config.parameters

        match config.action:
            case IntentAction.CREATE_ROUTE | IntentAction.UPDATE_ROUTE:
                weighted = None
                if "weighted_clusters" in params:
                    weighted = [
                        WeightedCluster(**wc) for wc in params["weighted_clusters"]
                    ]
                return RouteConfig(
                    route_name=params.get("route_name", f"{config.target_service}-route"),
                    target_cluster=params.get("target_cluster"),
                    weighted_clusters=weighted,
                    timeout_ms=params.get("timeout_ms", 15000),
                    retry_on=params.get("retry_on"),
                    max_retries=params.get("max_retries", 1),
                )

            case IntentAction.CONFIGURE_LOAD_BALANCING:
                lb_policy = params.get("lb_policy", "ROUND_ROBIN")
                return ClusterConfig(
                    cluster_name=params.get("cluster_name", config.target_service),
                    lb_policy=LoadBalancingPolicy(lb_policy),
                )

            case IntentAction.UPDATE_RATE_LIMIT:
                return RateLimitConfig(
                    name=params.get("name", f"{config.target_service}-rate-limit"),
                    target_route=params.get("target_route", "/"),
                    requests_per_unit=params.get("requests_per_unit", 1000),
                    unit=params.get("unit", "minute"),
                )

            case IntentAction.CREATE_CLUSTER | IntentAction.UPDATE_CLUSTER:
                return ClusterConfig(
                    cluster_name=params.get("cluster_name", config.target_service),
                    lb_policy=LoadBalancingPolicy(params.get("lb_policy", "ROUND_ROBIN")),
                )

            case _:
                # Actions like scale_service, configure_timeout, etc.
                # don't map 1:1 to a Sovereign schema
                return None
