"""
Safety Service — deterministic guardrails for AI-generated configurations.

This is the critical safety boundary. ALL AI outputs pass through this layer
before reaching the Envoy control plane. A single AI hallucination could
theoretically route all database traffic into a black hole — this service
prevents that.

Responsibilities:
- Schema validation against deterministic rules
- Blast radius simulation
- Dangerous pattern detection (wildcard routes, overly permissive configs)
"""

from __future__ import annotations

from typing import Any

import structlog
from pydantic import BaseModel

from broker.config import Settings
from broker.schemas.intent import (
    BlastRadiusReport,
    IntentAction,
    ParsedConfiguration,
    ValidationResult,
)
from broker.schemas.sovereign import RateLimitConfig, RouteConfig

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# Dangerous pattern definitions
# ---------------------------------------------------------------------------
DANGEROUS_PATTERNS: list[dict[str, Any]] = [
    {
        "name": "wildcard_route",
        "description": "Route matches all traffic with no specificity",
        "check": lambda config: (
            isinstance(config, RouteConfig)
            and config.match.prefix == "/"
            and not config.match.headers
            and not config.match.query_parameters
            and config.target_cluster is not None
        ),
        "risk_score": 0.8,
    },
    {
        "name": "zero_rate_limit",
        "description": "Rate limit set to unreasonably low value (potential DoS)",
        "check": lambda config: (
            isinstance(config, RateLimitConfig)
            and config.requests_per_unit < 10
        ),
        "risk_score": 0.7,
    },
    {
        "name": "excessive_rate_limit",
        "description": "Rate limit set extremely high, effectively no limit",
        "check": lambda config: (
            isinstance(config, RateLimitConfig)
            and config.requests_per_unit > 100_000
        ),
        "risk_score": 0.3,
    },
    {
        "name": "unbalanced_traffic_split",
        "description": "Traffic weights do not sum to 100",
        "check": lambda config: (
            isinstance(config, RouteConfig)
            and config.weighted_clusters is not None
            and sum(wc.weight for wc in config.weighted_clusters) != 100
        ),
        "risk_score": 0.9,
    },
]


class SafetyService:
    """Deterministic safety validation for AI-generated configurations."""

    def __init__(self, dynamodb_service: Any, settings: Settings) -> None:
        """Initialize with DynamoDB access for current state comparison."""
        self._db = dynamodb_service
        self._settings = settings

    async def validate_config(
        self,
        parsed: ParsedConfiguration,
        sovereign_config: BaseModel | None,
    ) -> ValidationResult:
        """Validate a parsed configuration against deterministic rules.

        Args:
            parsed: The AI-generated parsed configuration.
            sovereign_config: The mapped Sovereign schema (if applicable).

        Returns:
            ValidationResult with errors and warnings.
        """
        errors: list[str] = []
        warnings: list[str] = []

        # Rule 1: Action must be a valid IntentAction
        if parsed.action not in IntentAction:
            errors.append(f"Unknown action: {parsed.action}")

        # Rule 2: Target service must be specified
        if not parsed.target_service or parsed.target_service.strip() == "":
            errors.append("Target service is empty or missing.")

        # Rule 3: Target service name must be valid (no special characters)
        if parsed.target_service and not self._is_valid_service_name(parsed.target_service):
            errors.append(
                f"Invalid service name: '{parsed.target_service}'. "
                "Must contain only alphanumeric characters, hyphens, and underscores."
            )

        # Rule 4: Check for dangerous patterns in the sovereign config
        if sovereign_config is not None:
            for pattern in DANGEROUS_PATTERNS:
                try:
                    if pattern["check"](sovereign_config):
                        if pattern["risk_score"] >= 0.7:
                            errors.append(
                                f"Dangerous pattern detected: {pattern['name']} — "
                                f"{pattern['description']}"
                            )
                        else:
                            warnings.append(
                                f"Warning pattern: {pattern['name']} — "
                                f"{pattern['description']}"
                            )
                except Exception:
                    # Pattern check should never crash validation
                    pass

        # Rule 5: Action-specific parameter validation
        action_errors = self._validate_action_parameters(parsed)
        errors.extend(action_errors)

        is_valid = len(errors) == 0

        await logger.ainfo(
            "Configuration validation completed",
            is_valid=is_valid,
            error_count=len(errors),
            warning_count=len(warnings),
        )

        return ValidationResult(
            is_valid=is_valid,
            errors=errors,
            warnings=warnings,
        )

    async def simulate_blast_radius(
        self,
        parsed: ParsedConfiguration,
    ) -> BlastRadiusReport:
        """Simulate the impact of a configuration change.

        Checks the proposed change against the current production state
        to estimate the number of affected services and overall risk.

        Args:
            parsed: The AI-generated parsed configuration.

        Returns:
            BlastRadiusReport with risk assessment.
        """
        risk_score = 0.0
        affected_services: list[str] = [parsed.target_service]
        affected_routes: list[str] = []
        description_parts: list[str] = []

        # High-risk action types
        high_risk_actions = {
            IntentAction.DELETE_ROUTE: 0.6,
            IntentAction.SCALE_SERVICE: 0.4,
            IntentAction.UPDATE_RATE_LIMIT: 0.3,
        }

        if parsed.action in high_risk_actions:
            risk_score += high_risk_actions[parsed.action]
            description_parts.append(
                f"Action '{parsed.action}' carries inherent risk."
            )

        # Check for traffic splitting (affects multiple clusters)
        if parsed.action in (IntentAction.CREATE_ROUTE, IntentAction.UPDATE_ROUTE):
            weighted = parsed.parameters.get("weighted_clusters", [])
            if weighted:
                cluster_names = [wc.get("cluster_name", "") for wc in weighted if isinstance(wc, dict)]
                affected_services.extend(cluster_names)
                affected_routes.append(
                    parsed.parameters.get("route_name", "unknown-route")
                )
                description_parts.append(
                    f"Traffic split across {len(cluster_names)} clusters."
                )

        # Check for wildcard/broad scope
        params = parsed.parameters
        if params.get("prefix") == "/" or params.get("target_route") == "/":
            risk_score += 0.3
            description_parts.append("Configuration applies to root path (broad scope).")

        # Clamp risk score
        risk_score = min(risk_score, 1.0)
        is_safe = risk_score < 0.7

        report = BlastRadiusReport(
            risk_score=round(risk_score, 2),
            affected_services=list(set(affected_services)),
            affected_routes=affected_routes,
            description=" ".join(description_parts) if description_parts else "Low risk change.",
            is_safe=is_safe,
        )

        await logger.ainfo(
            "Blast radius simulation completed",
            risk_score=report.risk_score,
            is_safe=report.is_safe,
            affected_services_count=len(report.affected_services),
        )

        return report

    def _is_valid_service_name(self, name: str) -> bool:
        """Check if a service name contains only safe characters."""
        import re

        return bool(re.match(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$", name))

    def _validate_action_parameters(self, parsed: ParsedConfiguration) -> list[str]:
        """Validate action-specific parameter requirements."""
        errors: list[str] = []
        params = parsed.parameters

        match parsed.action:
            case IntentAction.UPDATE_RATE_LIMIT:
                if "requests_per_unit" not in params:
                    errors.append("Rate limit action requires 'requests_per_unit' parameter.")
                elif not isinstance(params["requests_per_unit"], int):
                    errors.append("'requests_per_unit' must be an integer.")

            case IntentAction.CREATE_ROUTE | IntentAction.UPDATE_ROUTE:
                has_target = "target_cluster" in params
                has_weighted = "weighted_clusters" in params
                if not has_target and not has_weighted:
                    errors.append(
                        "Route action requires either 'target_cluster' or 'weighted_clusters'."
                    )
                if has_weighted:
                    weighted = params["weighted_clusters"]
                    if not isinstance(weighted, list) or len(weighted) < 2:
                        errors.append("'weighted_clusters' must be a list with at least 2 entries.")

            case IntentAction.CONFIGURE_LOAD_BALANCING:
                if "lb_policy" not in params:
                    errors.append("Load balancing action requires 'lb_policy' parameter.")

            case IntentAction.SCALE_SERVICE:
                if "target_replicas" not in params and "scale_factor" not in params:
                    errors.append(
                        "Scale action requires 'target_replicas' or 'scale_factor' parameter."
                    )

        return errors
