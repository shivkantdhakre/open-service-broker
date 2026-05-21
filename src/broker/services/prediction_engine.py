"""
Prediction Engine — ML-based traffic forecasting and anomaly detection.

Uses:
- Time-series forecasting (linear regression / Prophet-style) for predicting
  traffic spikes before they happen.
- Isolation Forest (scikit-learn) for real-time anomaly detection.

The engine combines predictive scaling for known patterns with reactive
anomaly detection as a safety net.
"""

from __future__ import annotations

import asyncio

import numpy as np
import structlog
from sklearn.ensemble import IsolationForest  # type: ignore[import-untyped]
from sklearn.linear_model import LinearRegression  # type: ignore[import-untyped]
from ulid import ULID

from broker.config import Settings
from broker.schemas.metrics import (
    AnomalyAlert,
    AnomalySeverity,
    ScalingAction,
    ScalingPrediction,
    TrafficMetric,
)

logger = structlog.get_logger()


class PredictionEngine:
    """ML-based traffic prediction and anomaly detection engine."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._anomaly_detectors: dict[str, IsolationForest] = {}
        self._active_anomalies: dict[str, AnomalyAlert] = {}
        self._is_warmed_up: bool = False

    async def warm_up(
        self,
        metrics_collector: Any,
        window_hours: int = 24,
    ) -> dict[str, int]:
        """Pre-train anomaly detectors from historical metrics.

        Call this on startup to avoid the cold-start problem where
        IsolationForest detectors have no training data and cannot
        detect anomalies until enough real-time data accumulates.

        Args:
            metrics_collector: A MetricsCollector instance with
                get_all_services() and get_historical_metrics() methods.
            window_hours: Hours of history to pull for training.

        Returns:
            Dict mapping service names to the number of data points
            used for training.
        """
        training_report: dict[str, int] = {}

        try:
            services = await metrics_collector.get_all_services()

            await logger.ainfo(
                "Cold start warm-up: loading historical metrics",
                services_found=len(services),
                window_hours=window_hours,
            )

            for service_name in services:
                metrics = await metrics_collector.get_historical_metrics(
                    service_name,
                    window_hours=window_hours,
                )

                if len(metrics) >= 10:
                    # Train in thread pool to avoid blocking
                    await asyncio.to_thread(
                        self.train_anomaly_detector,
                        service_name,
                        metrics,
                    )
                    training_report[service_name] = len(metrics)

                    await logger.ainfo(
                        "Anomaly detector trained",
                        service_name=service_name,
                        data_points=len(metrics),
                    )
                else:
                    training_report[service_name] = 0
                    await logger.awarning(
                        "Insufficient data for warm-up",
                        service_name=service_name,
                        data_points=len(metrics),
                        required=10,
                    )

            self._is_warmed_up = True

            await logger.ainfo(
                "Cold start warm-up complete",
                services_trained=sum(
                    1 for v in training_report.values() if v > 0
                ),
                total_services=len(training_report),
            )

        except Exception as e:
            await logger.aerror(
                "Cold start warm-up failed (non-fatal)",
                error=str(e),
            )

        return training_report

    @property
    def is_warmed_up(self) -> bool:
        """Whether the engine has been pre-trained with historical data."""
        return self._is_warmed_up

    async def predict(
        self,
        service_name: str,
        historical_metrics: list[TrafficMetric],
        horizon_minutes: int | None = None,
    ) -> ScalingPrediction:
        """Predict future traffic load for a service.

        Uses linear regression on recent metrics to forecast the load
        at the specified horizon. Falls back to simple averaging for
        insufficient data.

        Args:
            service_name: The service to predict for.
            historical_metrics: Historical traffic data.
            horizon_minutes: How far ahead to predict (default from settings).

        Returns:
            ScalingPrediction with recommended action.
        """
        horizon = horizon_minutes or self._settings.scaling_prediction_horizon_minutes

        if len(historical_metrics) < 3:
            # Insufficient data — return no-action with low confidence
            current = historical_metrics[-1].requests_per_second if historical_metrics else 0
            return ScalingPrediction(
                service_name=service_name,
                predicted_load=current,
                current_load=current,
                confidence=0.1,
                recommended_action=ScalingAction.NO_ACTION,
                horizon_minutes=horizon,
                reasoning="Insufficient historical data for prediction (need at least 3 data points).",
            )

        # Run ML prediction in a thread to avoid blocking the event loop
        prediction = await asyncio.to_thread(
            self._run_prediction,
            service_name,
            historical_metrics,
            horizon,
        )

        return prediction

    def _run_prediction(
        self,
        service_name: str,
        metrics: list[TrafficMetric],
        horizon_minutes: int,
    ) -> ScalingPrediction:
        """Execute the prediction model (runs in thread pool)."""
        # Extract time-series data
        timestamps = np.array([m.timestamp.timestamp() for m in metrics])
        loads = np.array([m.requests_per_second for m in metrics])

        # Normalize timestamps to minutes from start
        t_start = timestamps[0]
        t_normalized = (timestamps - t_start) / 60.0

        # Linear regression for trend prediction
        x_features = t_normalized.reshape(-1, 1)
        model = LinearRegression()
        model.fit(x_features, loads)

        # Predict at horizon
        t_future = np.array([[t_normalized[-1] + horizon_minutes]])
        predicted_load = max(0.0, float(model.predict(t_future)[0]))

        current_load = float(loads[-1])

        # Calculate R² score as confidence proxy
        r2_score = max(0.0, float(model.score(x_features, loads)))
        confidence = min(r2_score * 0.8 + 0.2, 1.0)  # Scale to [0.2, 1.0]

        # Determine recommended action
        load_ratio = predicted_load / max(current_load, 1.0)
        action, reasoning = self._determine_action(load_ratio, predicted_load, current_load)

        return ScalingPrediction(
            service_name=service_name,
            predicted_load=round(predicted_load, 2),
            current_load=round(current_load, 2),
            confidence=round(confidence, 3),
            recommended_action=action,
            horizon_minutes=horizon_minutes,
            reasoning=reasoning,
            metadata={
                "r2_score": round(r2_score, 4),
                "slope": round(float(model.coef_[0]), 4),
                "intercept": round(float(model.intercept_), 4),
                "data_points": len(metrics),
            },
        )

    def _determine_action(
        self,
        load_ratio: float,
        predicted: float,
        current: float,
    ) -> tuple[ScalingAction, str]:
        """Determine the recommended scaling action based on load ratio."""
        if load_ratio > 1.5:
            return (
                ScalingAction.SCALE_UP,
                f"Predicted load ({predicted:.0f} rps) is {load_ratio:.1f}x current "
                f"({current:.0f} rps). Pre-emptive scale-up recommended.",
            )
        elif load_ratio > 1.2:
            return (
                ScalingAction.INVESTIGATE,
                f"Predicted load ({predicted:.0f} rps) is {load_ratio:.1f}x current "
                f"({current:.0f} rps). Monitor closely and prepare to scale.",
            )
        elif load_ratio < 0.5:
            return (
                ScalingAction.SCALE_DOWN,
                f"Predicted load ({predicted:.0f} rps) is {load_ratio:.1f}x current "
                f"({current:.0f} rps). Scale-down may reduce costs.",
            )
        else:
            return (
                ScalingAction.NO_ACTION,
                f"Predicted load ({predicted:.0f} rps) is stable relative to current "
                f"({current:.0f} rps). No scaling action needed.",
            )

    async def detect_anomaly(self, metric: TrafficMetric) -> AnomalyAlert | None:
        """Detect anomalies in a real-time metric using Isolation Forest.

        The detector is trained on historical data and flags data points
        that deviate significantly from learned patterns.

        Args:
            metric: The incoming metric to evaluate.

        Returns:
            AnomalyAlert if an anomaly is detected, None otherwise.
        """
        return await asyncio.to_thread(self._run_anomaly_detection, metric)

    def _run_anomaly_detection(self, metric: TrafficMetric) -> AnomalyAlert | None:
        """Run anomaly detection (in thread pool)."""
        service = metric.service_name

        # Get or create detector for this service
        if service not in self._anomaly_detectors:
            self._anomaly_detectors[service] = IsolationForest(
                contamination=0.1,
                random_state=42,
                n_estimators=100,
            )
            # Not enough data to detect anomalies yet
            return None

        detector = self._anomaly_detectors[service]

        # Build feature vector
        features = np.array([[
            metric.requests_per_second,
            metric.latency_p99_ms,
            metric.error_rate,
            metric.cpu_usage,
            metric.memory_usage,
        ]])

        try:
            # Predict: 1 = normal, -1 = anomaly
            prediction = detector.predict(features)

            if prediction[0] == -1:
                # Determine which metric is most anomalous
                anomaly_scores = detector.decision_function(features)
                score = float(anomaly_scores[0])

                # Determine severity based on anomaly score
                severity = self._score_to_severity(score)

                # Find the most anomalous metric
                metric_names = [
                    "requests_per_second",
                    "latency_p99_ms",
                    "error_rate",
                    "cpu_usage",
                    "memory_usage",
                ]
                values = features[0]
                max_idx = int(np.argmax(np.abs(values)))
                anomalous_metric = metric_names[max_idx]
                observed_value = float(values[max_idx])

                alert = AnomalyAlert(
                    alert_id=str(ULID()),
                    service_name=service,
                    metric_name=anomalous_metric,
                    observed_value=observed_value,
                    expected_min=0.0,
                    expected_max=observed_value * 0.8,  # Rough estimate
                    severity=severity,
                    description=(
                        f"Anomalous {anomalous_metric} detected for {service}: "
                        f"{observed_value:.2f} (anomaly score: {score:.4f})"
                    ),
                )

                self._active_anomalies[alert.alert_id] = alert
                return alert

        except Exception:
            # If the detector hasn't been fitted yet, this is expected
            pass

        return None

    def _score_to_severity(self, score: float) -> AnomalySeverity:
        """Map Isolation Forest anomaly score to severity level."""
        # More negative = more anomalous
        if score < -0.5:
            return AnomalySeverity.CRITICAL
        elif score < -0.3:
            return AnomalySeverity.HIGH
        elif score < -0.1:
            return AnomalySeverity.MEDIUM
        else:
            return AnomalySeverity.LOW

    def train_anomaly_detector(
        self,
        service_name: str,
        historical_metrics: list[TrafficMetric],
    ) -> None:
        """Train the anomaly detector on historical data.

        Args:
            service_name: The service to train for.
            historical_metrics: Historical data to learn normal patterns from.
        """
        if len(historical_metrics) < 10:
            return  # Need minimum data to train

        features = np.array([
            [
                m.requests_per_second,
                m.latency_p99_ms,
                m.error_rate,
                m.cpu_usage,
                m.memory_usage,
            ]
            for m in historical_metrics
        ])

        detector = IsolationForest(
            contamination=0.1,
            random_state=42,
            n_estimators=100,
        )
        detector.fit(features)
        self._anomaly_detectors[service_name] = detector

    def get_active_anomalies(self) -> list[AnomalyAlert]:
        """Return all currently active (unacknowledged) anomaly alerts."""
        return [
            alert for alert in self._active_anomalies.values()
            if not alert.is_acknowledged
        ]

    def acknowledge_anomaly(self, alert_id: str) -> bool:
        """Acknowledge an anomaly alert."""
        alert = self._active_anomalies.get(alert_id)
        if alert:
            alert.is_acknowledged = True
            return True
        return False
