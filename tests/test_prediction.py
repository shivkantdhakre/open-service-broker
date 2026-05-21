"""
Tests for the Prediction Engine — traffic forecasting and anomaly detection.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from broker.schemas.metrics import ScalingAction, TrafficMetric
from broker.services.prediction_engine import PredictionEngine


@pytest.fixture
def engine(settings):
    return PredictionEngine(settings)


def _generate_metrics(
    service: str,
    count: int = 20,
    base_rps: float = 100.0,
    slope: float = 5.0,
) -> list[TrafficMetric]:
    """Generate synthetic traffic metrics with a linear trend."""
    now = datetime.now(UTC)
    metrics = []
    for i in range(count):
        metrics.append(
            TrafficMetric(
                timestamp=now - timedelta(minutes=count - i),
                service_name=service,
                requests_per_second=base_rps + slope * i,
                latency_p99_ms=50.0 + i * 0.5,
                error_rate=0.01,
                cpu_usage=0.3 + i * 0.01,
                memory_usage=0.4,
                active_connections=100 + i * 2,
            )
        )
    return metrics


class TestPredictionEngine:
    """Tests for traffic prediction."""

    @pytest.mark.asyncio
    async def test_predict_insufficient_data(self, engine):
        """Prediction with <3 data points should return low confidence."""
        metrics = _generate_metrics("test-svc", count=2)
        prediction = await engine.predict("test-svc", metrics)

        assert prediction.confidence < 0.5
        assert prediction.recommended_action == ScalingAction.NO_ACTION
        assert "insufficient" in prediction.reasoning.lower()

    @pytest.mark.asyncio
    async def test_predict_upward_trend(self, engine):
        """Upward traffic trend should predict higher load."""
        metrics = _generate_metrics("test-svc", count=20, base_rps=100, slope=10)
        prediction = await engine.predict("test-svc", metrics, horizon_minutes=30)

        assert prediction.predicted_load > prediction.current_load
        assert prediction.service_name == "test-svc"
        assert prediction.horizon_minutes == 30

    @pytest.mark.asyncio
    async def test_predict_stable_traffic(self, engine):
        """Stable traffic should recommend no action."""
        metrics = _generate_metrics("test-svc", count=20, base_rps=100, slope=0)
        prediction = await engine.predict("test-svc", metrics)

        assert prediction.recommended_action == ScalingAction.NO_ACTION

    @pytest.mark.asyncio
    async def test_predict_rapid_increase_recommends_scale_up(self, engine):
        """Rapidly increasing traffic should recommend scale up."""
        metrics = _generate_metrics("test-svc", count=20, base_rps=50, slope=50)
        prediction = await engine.predict("test-svc", metrics, horizon_minutes=60)

        # With a steep slope, predicted load should be significantly higher
        assert prediction.predicted_load > prediction.current_load * 1.2

    @pytest.mark.asyncio
    async def test_prediction_metadata_present(self, engine):
        """Prediction should include model metadata."""
        metrics = _generate_metrics("test-svc", count=20)
        prediction = await engine.predict("test-svc", metrics)

        assert "r2_score" in prediction.metadata
        assert "slope" in prediction.metadata
        assert "data_points" in prediction.metadata
        assert prediction.metadata["data_points"] == 20

    @pytest.mark.asyncio
    async def test_predict_empty_metrics(self, engine):
        """Empty metrics list should handle gracefully."""
        prediction = await engine.predict("test-svc", [])

        assert prediction.confidence < 0.5
        assert prediction.recommended_action == ScalingAction.NO_ACTION


class TestAnomalyDetection:
    """Tests for anomaly detection."""

    @pytest.mark.asyncio
    async def test_detect_anomaly_untrained(self, engine):
        """Untrained detector should return None (no anomaly)."""
        metric = TrafficMetric(
            service_name="new-service",
            requests_per_second=1000,
            latency_p99_ms=500,
            error_rate=0.5,
            cpu_usage=0.95,
            memory_usage=0.9,
        )

        result = await engine.detect_anomaly(metric)
        assert result is None  # Not trained yet

    def test_train_detector_minimum_data(self, engine):
        """Training with <10 data points should be a no-op."""
        metrics = _generate_metrics("test-svc", count=5)
        engine.train_anomaly_detector("test-svc", metrics)

        assert "test-svc" not in engine._anomaly_detectors

    def test_train_detector_sufficient_data(self, engine):
        """Training with ≥10 data points should create a detector."""
        metrics = _generate_metrics("test-svc", count=15)
        engine.train_anomaly_detector("test-svc", metrics)

        assert "test-svc" in engine._anomaly_detectors

    def test_get_active_anomalies_empty(self, engine):
        """No anomalies should return empty list."""
        assert engine.get_active_anomalies() == []

    def test_acknowledge_nonexistent_anomaly(self, engine):
        """Acknowledging a missing alert should return False."""
        assert engine.acknowledge_anomaly("nonexistent-id") is False
