"""
CloudWatch Metrics Exporter — exports EventBus metrics to AWS CloudWatch.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    import aioboto3

    from broker.config import Settings
    from broker.services.event_bus import EventBus

logger = structlog.get_logger()


class CloudWatchMetricsExporter:
    """Exports metrics from the EventBus to AWS CloudWatch."""

    def __init__(
        self,
        event_bus: EventBus,
        session: aioboto3.Session,
        settings: Settings,
        namespace: str = "OSB/Broker",
        interval: float = 60.0,
    ) -> None:
        self.event_bus = event_bus
        self.session = session
        self.settings = settings
        self.namespace = namespace
        self.interval = interval
        self.task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Start the metrics export loop."""
        self.task = asyncio.create_task(self._run_loop())
        await logger.ainfo(
            "Started CloudWatch metrics exporter background task",
            interval=self.interval,
            namespace=self.namespace,
        )

    async def stop(self) -> None:
        """Stop the metrics export loop."""
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
            await logger.ainfo("Stopped CloudWatch metrics exporter background task")

    async def _run_loop(self) -> None:
        """Export metrics periodically."""
        try:
            while True:
                await asyncio.sleep(self.interval)
                await self.export_metrics()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            await logger.aerror("CloudWatch metrics exporter loop crashed", error=str(e))

    async def export_metrics(self) -> None:
        """Fetch metrics from the event bus and put them to CloudWatch."""
        metrics = self.event_bus.metrics
        metric_data = []
        for name, value in metrics.items():
            metric_data.append({
                "MetricName": name,
                "Value": float(value),
                "Unit": "Count",
            })

        if not metric_data:
            return

        try:
            async with self.session.client(
                "cloudwatch",
                endpoint_url=self.settings.aws_endpoint_url,
                region_name=self.settings.aws_region,
            ) as cw:
                await cw.put_metric_data(
                    Namespace=self.namespace,
                    MetricData=metric_data,
                )
            await logger.adebug("Successfully exported metrics to CloudWatch", metrics=metrics)
        except Exception as e:
            await logger.aerror("Failed to export metrics to CloudWatch", error=str(e))
