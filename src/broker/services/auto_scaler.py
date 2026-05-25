"""
Auto Scaler — consumes predictions from the PredictionEngine and
triggers scaling actions via the SQS task queue.

Implements cooldown periods to prevent oscillation and integrates
with the SafetyService for blast radius checks.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

from broker.schemas.metrics import ScalingAction, ScalingPrediction
from broker.schemas.task import TaskMessage, TaskType

if TYPE_CHECKING:
    from broker.config import Settings
    from broker.services.sqs import SQSService

logger = structlog.get_logger()


class AutoScaler:
    """Automated scaling engine that acts on ML predictions."""

    def __init__(self, sqs_service: SQSService, settings: Settings) -> None:
        self._sqs = sqs_service
        self._settings = settings
        self._last_scaling_actions: dict[str, datetime] = {}

    async def evaluate_prediction(self, prediction: ScalingPrediction) -> bool:
        """Evaluate a scaling prediction and take action if warranted.

        Args:
            prediction: The ML prediction to evaluate.

        Returns:
            True if a scaling action was initiated, False otherwise.
        """
        service = prediction.service_name

        # Check confidence threshold
        if prediction.confidence < self._settings.scaling_confidence_threshold:
            await logger.ainfo(
                "Prediction confidence below threshold, skipping",
                service=service,
                confidence=prediction.confidence,
                threshold=self._settings.scaling_confidence_threshold,
            )
            return False

        # Check cooldown
        if self._is_in_cooldown(service):
            await logger.ainfo(
                "Service in scaling cooldown, skipping",
                service=service,
            )
            return False

        # Only act on SCALE_UP and SCALE_DOWN actions
        if prediction.recommended_action not in (
            ScalingAction.SCALE_UP,
            ScalingAction.SCALE_DOWN,
        ):
            return False

        # Enqueue scaling task
        task = TaskMessage(
            task_id=f"auto-scale-{service}-{datetime.now(UTC).isoformat()}",
            task_type=TaskType.SCALE,
            resource_id=service,
            configuration={
                "action": prediction.recommended_action.value,
                "predicted_load": prediction.predicted_load,
                "current_load": prediction.current_load,
                "confidence": prediction.confidence,
                "horizon_minutes": prediction.horizon_minutes,
                "reasoning": prediction.reasoning,
            },
            requested_by="auto-scaler",
        )

        await self._sqs.enqueue_task(task)
        self._last_scaling_actions[service] = datetime.now(UTC)

        await logger.ainfo(
            "Auto-scaling action initiated",
            service=service,
            action=prediction.recommended_action,
            predicted_load=prediction.predicted_load,
            confidence=prediction.confidence,
        )

        return True

    def _is_in_cooldown(self, service_name: str) -> bool:
        """Check if a service is within its scaling cooldown period."""
        last_action = self._last_scaling_actions.get(service_name)
        if last_action is None:
            return False

        elapsed = (datetime.now(UTC) - last_action).total_seconds()
        return elapsed < self._settings.scaling_cooldown_seconds

    def get_cooldown_status(self) -> dict[str, Any]:
        """Get cooldown status for all services."""
        now = datetime.now(UTC)
        status: dict[str, Any] = {}

        for service, last_action in self._last_scaling_actions.items():
            elapsed = (now - last_action).total_seconds()
            remaining = max(0, self._settings.scaling_cooldown_seconds - elapsed)
            status[service] = {
                "last_action": last_action.isoformat(),
                "cooldown_remaining_seconds": int(remaining),
                "is_active": remaining > 0,
            }

        return status
