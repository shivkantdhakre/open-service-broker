"""
Scaling Router — API endpoints for predictive scaling and observability.

GET  /predictions            — Current predictions for all services
GET  /predictions/{service}  — Prediction for a specific service
GET  /anomalies              — Active anomaly alerts
POST /simulate               — Dry-run a scaling scenario
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, HTTPException, Request, status

from broker.config import get_settings
from broker.schemas.metrics import (
    AnomalyAlert,
    ScalingPrediction,
    ScalingSimulationRequest,
    ScalingSimulationResult,
)
from broker.services.prediction_engine import PredictionEngine

logger = structlog.get_logger()

router = APIRouter()

# Module-level prediction engine (initialized lazily)
_prediction_engine: PredictionEngine | None = None


def _get_prediction_engine() -> PredictionEngine:
    """Get or create the prediction engine singleton."""
    global _prediction_engine
    if _prediction_engine is None:
        _prediction_engine = PredictionEngine(get_settings())
    return _prediction_engine


@router.get(
    "/predictions",
    response_model=list[ScalingPrediction],
    summary="Get scaling predictions for all services",
    description=(
        "Returns ML-based traffic predictions and scaling recommendations "
        "for all monitored services."
    ),
)
async def get_all_predictions(request: Request) -> list[ScalingPrediction]:
    """Get predictions for all monitored services."""
    _get_prediction_engine()  # Ensure engine is initialized

    # For now, return empty list if no metrics are available
    # In production, this would query MetricsCollector for all services
    return []


@router.get(
    "/predictions/{service_name}",
    response_model=ScalingPrediction,
    summary="Get scaling prediction for a specific service",
)
async def get_service_prediction(
    service_name: str,
    horizon_minutes: int = 30,
    request: Request = None,  # type: ignore[assignment]
) -> ScalingPrediction:
    """Get a traffic prediction for a specific service."""
    engine = _get_prediction_engine()

    # In production, this would fetch real metrics from MetricsCollector
    # For now, return a prediction with no data
    prediction = await engine.predict(
        service_name=service_name,
        historical_metrics=[],
        horizon_minutes=horizon_minutes,
    )

    return prediction


@router.get(
    "/anomalies",
    response_model=list[AnomalyAlert],
    summary="Get active anomaly alerts",
    description="Returns all currently active (unacknowledged) anomaly alerts.",
)
async def get_anomalies() -> list[AnomalyAlert]:
    """Get active anomaly alerts across all services."""
    engine = _get_prediction_engine()
    return engine.get_active_anomalies()


@router.post(
    "/anomalies/{alert_id}/acknowledge",
    summary="Acknowledge an anomaly alert",
)
async def acknowledge_anomaly(alert_id: str) -> dict[str, str]:
    """Acknowledge an anomaly alert to suppress it."""
    engine = _get_prediction_engine()
    if engine.acknowledge_anomaly(alert_id):
        return {"status": "acknowledged", "alert_id": alert_id}
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"type": "alert_not_found", "title": f"No alert with ID '{alert_id}' exists."},
    )


@router.post(
    "/simulate",
    response_model=ScalingSimulationResult,
    summary="Simulate a scaling scenario",
    description="Dry-run a scaling scenario to preview impact before applying.",
)
async def simulate_scaling(
    request: ScalingSimulationRequest,
) -> ScalingSimulationResult:
    """Simulate a scaling scenario without applying changes."""
    # Simulated result — in production, this would use actual capacity data
    current_replicas = 3  # Would be fetched from infrastructure

    if request.target_replicas:
        proposed = request.target_replicas
    elif request.scale_factor:
        proposed = max(1, int(current_replicas * request.scale_factor))
    else:
        proposed = current_replicas

    # Estimate impact
    if proposed > current_replicas:
        impact = f"Capacity increase of {((proposed / current_replicas) - 1) * 100:.0f}%"
        cost = f"Estimated cost increase: ~${(proposed - current_replicas) * 50}/month"
        risk = "Low risk — scaling up is generally safe"
    elif proposed < current_replicas:
        impact = f"Capacity decrease of {(1 - (proposed / current_replicas)) * 100:.0f}%"
        cost = f"Estimated cost savings: ~${(current_replicas - proposed) * 50}/month"
        risk = "Medium risk — ensure current load can be handled by fewer replicas"
    else:
        impact = "No change in capacity"
        cost = "No cost change"
        risk = "No risk"

    return ScalingSimulationResult(
        service_name=request.service_name,
        current_replicas=current_replicas,
        proposed_replicas=proposed,
        predicted_impact=impact,
        cost_estimate=cost,
        risk_assessment=risk,
    )
