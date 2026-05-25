"""
Metrics Collector — ingests and stores traffic metrics for the prediction engine.

Collects metrics from Envoy proxy stats or Prometheus endpoints,
stores time-series data in DynamoDB with TTL for automatic cleanup,
and provides historical data access for the prediction engine.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog

from broker.schemas.metrics import TrafficMetric

if TYPE_CHECKING:
    from broker.config import Settings

logger = structlog.get_logger()


class MetricsCollector:
    """Collects and stores traffic metrics in DynamoDB."""

    def __init__(self, dynamodb_resource: Any, settings: Settings) -> None:
        self._dynamodb = dynamodb_resource
        self._settings = settings
        self._table_name = settings.dynamodb_metrics_table_name

    async def _get_table(self) -> Any:
        """Get the DynamoDB metrics table."""
        return await self._dynamodb.Table(self._table_name)

    async def record_metric(self, metric: TrafficMetric) -> None:
        """Store a single traffic metric data point.

        Args:
            metric: The metric to store.
        """
        table = await self._get_table()
        item = metric.to_dynamodb_item()

        # Add TTL for automatic cleanup (30 days)
        ttl = int((metric.timestamp + timedelta(days=30)).timestamp())
        item["ttl"] = ttl

        await table.put_item(Item=item)

    async def record_metrics_batch(self, metrics: list[TrafficMetric]) -> None:
        """Store multiple metrics in a batch write.

        Args:
            metrics: List of metrics to store.
        """
        table = await self._get_table()

        async with table.batch_writer() as batch:
            for metric in metrics:
                item = metric.to_dynamodb_item()
                ttl = int((metric.timestamp + timedelta(days=30)).timestamp())
                item["ttl"] = ttl
                await batch.put_item(Item=item)

        await logger.ainfo(
            "Batch metrics recorded",
            count=len(metrics),
        )

    async def get_historical_metrics(
        self,
        service_name: str,
        window_hours: int = 24,
    ) -> list[TrafficMetric]:
        """Retrieve historical metrics for a service within a time window.

        Args:
            service_name: The service to query metrics for.
            window_hours: Number of hours of history to retrieve.

        Returns:
            List of TrafficMetric sorted by timestamp ascending.
        """
        from boto3.dynamodb.conditions import Key

        table = await self._get_table()
        cutoff = (datetime.now(UTC) - timedelta(hours=window_hours)).isoformat()

        response = await table.query(
            KeyConditionExpression=(
                Key("service_name").eq(service_name)
                & Key("timestamp").gte(cutoff)
            ),
            ScanIndexForward=True,  # Ascending order
        )

        items = response.get("Items", [])
        return [TrafficMetric.from_dynamodb_item(item) for item in items]

    async def get_latest_metric(self, service_name: str) -> TrafficMetric | None:
        """Get the most recent metric for a service.

        Args:
            service_name: The service to query.

        Returns:
            The latest TrafficMetric, or None.
        """
        from boto3.dynamodb.conditions import Key

        table = await self._get_table()

        response = await table.query(
            KeyConditionExpression=Key("service_name").eq(service_name),
            ScanIndexForward=False,  # Descending — latest first
            Limit=1,
        )

        items = response.get("Items", [])
        if items:
            return TrafficMetric.from_dynamodb_item(items[0])
        return None

    async def get_all_services(self) -> list[str]:
        """Get a list of all services with recorded metrics.

        Uses a scan (expensive) — should be cached in production.
        """
        table = await self._get_table()

        response = await table.scan(
            ProjectionExpression="service_name",
        )

        service_names = {item["service_name"] for item in response.get("Items", [])}
        return sorted(service_names)
