"""
Pydantic schemas for predictive scaling and AI observability.

Defines models for traffic metrics, scaling predictions, and anomaly alerts.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class AnomalySeverity(StrEnum):
    """Severity levels for anomaly alerts."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class ScalingAction(StrEnum):
    """Recommended scaling actions."""

    SCALE_UP = "SCALE_UP"
    SCALE_DOWN = "SCALE_DOWN"
    NO_ACTION = "NO_ACTION"
    INVESTIGATE = "INVESTIGATE"


class TrafficMetric(BaseModel):
    """A single traffic metric data point."""

    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    service_name: str
    requests_per_second: float = Field(ge=0)
    latency_p99_ms: float = Field(ge=0)
    error_rate: float = Field(ge=0, le=1.0)
    cpu_usage: float = Field(ge=0, le=1.0)
    memory_usage: float = Field(ge=0, le=1.0)
    active_connections: int = Field(ge=0, default=0)

    def to_dynamodb_item(self) -> dict[str, Any]:
        """Serialize for DynamoDB metrics table."""
        return {
            "service_name": self.service_name,
            "timestamp": self.timestamp.isoformat(),
            "requests_per_second": str(self.requests_per_second),
            "latency_p99_ms": str(self.latency_p99_ms),
            "error_rate": str(self.error_rate),
            "cpu_usage": str(self.cpu_usage),
            "memory_usage": str(self.memory_usage),
            "active_connections": self.active_connections,
        }

    @classmethod
    def from_dynamodb_item(cls, item: dict[str, Any]) -> TrafficMetric:
        """Deserialize from DynamoDB item."""
        return cls(
            service_name=item["service_name"],
            timestamp=datetime.fromisoformat(item["timestamp"]),
            requests_per_second=float(item.get("requests_per_second", 0)),
            latency_p99_ms=float(item.get("latency_p99_ms", 0)),
            error_rate=float(item.get("error_rate", 0)),
            cpu_usage=float(item.get("cpu_usage", 0)),
            memory_usage=float(item.get("memory_usage", 0)),
            active_connections=int(item.get("active_connections", 0)),
        )


class ScalingPrediction(BaseModel):
    """A prediction about future traffic and recommended scaling action."""

    service_name: str
    predicted_load: float = Field(
        ...,
        ge=0,
        description="Predicted requests per second at the forecast horizon.",
    )
    current_load: float = Field(ge=0, description="Current requests per second.")
    confidence: float = Field(ge=0, le=1.0)
    recommended_action: ScalingAction
    predicted_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    horizon_minutes: int = Field(default=30)
    reasoning: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class AnomalyAlert(BaseModel):
    """An alert for anomalous metric behavior."""

    alert_id: str
    service_name: str
    metric_name: str
    observed_value: float
    expected_min: float
    expected_max: float
    severity: AnomalySeverity
    detected_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    description: str = ""
    is_acknowledged: bool = False


class ScalingSimulationRequest(BaseModel):
    """Request for a scaling dry-run simulation."""

    service_name: str
    target_replicas: int | None = Field(default=None, ge=1)
    scale_factor: float | None = Field(default=None, gt=0)
    horizon_minutes: int = Field(default=30, ge=5, le=120)


class ScalingSimulationResult(BaseModel):
    """Result of a scaling simulation."""

    service_name: str
    current_replicas: int
    proposed_replicas: int
    predicted_impact: str
    cost_estimate: str
    risk_assessment: str
