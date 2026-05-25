"""
Pydantic schemas for the AI intent parsing pipeline.

Defines the request/response models for natural language → configuration
translation, including the structured output schema that the LLM must produce.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class IntentAction(StrEnum):
    """Supported configuration actions that can be parsed from natural language."""

    CREATE_ROUTE = "create_route"
    UPDATE_ROUTE = "update_route"
    DELETE_ROUTE = "delete_route"
    CONFIGURE_LOAD_BALANCING = "configure_load_balancing"
    UPDATE_RATE_LIMIT = "update_rate_limit"
    CREATE_CLUSTER = "create_cluster"
    UPDATE_CLUSTER = "update_cluster"
    SCALE_SERVICE = "scale_service"
    CONFIGURE_CIRCUIT_BREAKER = "configure_circuit_breaker"
    CONFIGURE_RETRY_POLICY = "configure_retry_policy"
    CONFIGURE_TIMEOUT = "configure_timeout"


# ---------------------------------------------------------------------------
# Request Models
# ---------------------------------------------------------------------------
class IntentRequest(BaseModel):
    """Developer's natural language request to the AI intent parser."""

    natural_language: str = Field(
        ...,
        min_length=5,
        max_length=2000,
        description="Plain English description of the desired infrastructure change.",
        examples=[
            "Route 30% of traffic from api-gateway to the canary deployment of user-service",
            "Set a rate limit of 1000 requests per minute on the /api/v1/payments endpoint",
            "Enable round-robin load balancing for the order-service cluster",
        ],
    )
    context: dict[str, Any] | None = Field(
        default=None,
        description="Optional context hints such as target environment, namespace, or service.",
        examples=[{"environment": "staging", "namespace": "payments"}],
    )


from broker.schemas.sovereign import WeightedCluster

class ActionParameters(BaseModel):
    """Specific parameters parsed by the LLM for Envoy/xDS configuration."""

    route_name: str | None = None
    target_cluster: str | None = None
    weighted_clusters: list[WeightedCluster] | None = None
    timeout_ms: int | None = None
    retry_on: str | None = None
    max_retries: int | None = None
    lb_policy: str | None = None
    cluster_name: str | None = None
    name: str | None = None
    target_route: str | None = None
    requests_per_unit: int | None = None
    unit: str | None = None
    prefix: str | None = None
    headers: dict[str, str] | None = None
    query_parameters: dict[str, str] | None = None

    def get(self, key: str, default: Any = None) -> Any:
        """Dict-like get method for backwards compatibility."""
        return self.model_dump(exclude_none=True).get(key, default)

    def __getitem__(self, key: str) -> Any:
        """Dict-like __getitem__ method for backwards compatibility."""
        return self.model_dump(exclude_none=True)[key]

    def __contains__(self, key: str) -> bool:
        """Dict-like __contains__ method for backwards compatibility."""
        return key in self.model_dump(exclude_none=True)

    def __iter__(self) -> Any:
        """Dict-like __iter__ method for backwards compatibility."""
        return iter(self.model_dump(exclude_none=True))

    def __eq__(self, other: Any) -> bool:
        """Dict-like __eq__ method for backwards compatibility."""
        if isinstance(other, dict):
            return self.model_dump(exclude_none=True) == other
        return super().__eq__(other)


# ---------------------------------------------------------------------------
# LLM Structured Output (what the LLM must produce)
# ---------------------------------------------------------------------------
class ParsedConfiguration(BaseModel):
    """Structured configuration output that the LLM must produce from NL input.

    This schema is provided to the LLM via structured outputs / function calling
    to guarantee valid, type-safe results.
    """

    action: IntentAction = Field(
        ...,
        description="The infrastructure action to perform.",
    )
    target_service: str = Field(
        ...,
        description="Name of the target service or cluster.",
    )
    parameters: ActionParameters = Field(
        default_factory=ActionParameters,
        description="Action-specific parameters (validated per action type downstream).",
    )
    reasoning: str = Field(
        ...,
        description="Brief explanation of how the natural language was interpreted.",
    )


# ---------------------------------------------------------------------------
# Safety / Validation Results
# ---------------------------------------------------------------------------
class ValidationResult(BaseModel):
    """Result of deterministic validation and blast radius analysis."""

    is_valid: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class BlastRadiusReport(BaseModel):
    """Assessment of the potential impact of a configuration change."""

    risk_score: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="0.0 = no risk, 1.0 = catastrophic.",
    )
    affected_services: list[str] = Field(default_factory=list)
    affected_routes: list[str] = Field(default_factory=list)
    description: str = ""
    is_safe: bool = True


# ---------------------------------------------------------------------------
# Response Models
# ---------------------------------------------------------------------------
class IntentResponse(BaseModel):
    """API response wrapping the parsed configuration with metadata."""

    request_id: str
    original_input: str
    parsed_configuration: ParsedConfiguration
    validation: ValidationResult
    blast_radius: BlastRadiusReport
    confidence_score: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Model's confidence in the translation.",
    )
    warnings: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class IntentApplyRequest(BaseModel):
    """Request to apply a previously parsed and validated configuration."""

    request_id: str = Field(..., description="ID from a prior /intent/parse response.")
    parsed_configuration: ParsedConfiguration
    force: bool = Field(
        default=False,
        description="If true, bypass blast radius warnings (requires elevated privileges).",
    )


class IntentHistoryItem(BaseModel):
    """Audit record of a past intent translation."""

    request_id: str
    original_input: str
    action: IntentAction
    target_service: str
    status: str
    created_at: datetime
    applied_at: datetime | None = None
