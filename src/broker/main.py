"""
FastAPI application factory and lifespan management.

Initializes AWS clients, event bus, and background tasks on startup.
Mounts all routers and middleware.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

import aioboto3
import structlog
import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from broker.config import get_settings
from broker.middleware.api_key import APIKeyMiddleware
from broker.middleware.error_handler import register_exception_handlers
from broker.middleware.logging import StructuredLoggingMiddleware
from broker.middleware.request_id import RequestIdMiddleware
from broker.routers import catalog, events, health, intent, maintenance, resources, scaling
from broker.services.cloudwatch_exporter import CloudWatchMetricsExporter
from broker.services.event_bus import Event, EventBus

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

# ---------------------------------------------------------------------------
# Structured logging configuration
# ---------------------------------------------------------------------------
structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.dev.set_exc_info,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG),
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
    cache_logger_on_first_use=True,
)

logger = structlog.get_logger()


async def poll_dlq_depth(
    event_bus: EventBus,
    session: aioboto3.Session,
    settings: Any,
    interval: float = 60.0,
) -> None:
    """Periodically check the SQS DLQ depth and publish warning events if messages are present."""
    await logger.ainfo("Starting DLQ poller background task", interval=interval)
    try:
        while True:
            await asyncio.sleep(interval)
            try:
                async with session.client(
                    "sqs",
                    endpoint_url=settings.aws_endpoint_url,
                    region_name=settings.aws_region,
                ) as sqs:
                    resp = await sqs.get_queue_attributes(
                        QueueUrl=settings.sqs_dlq_url,
                        AttributeNames=["ApproximateNumberOfMessages"],
                    )
                    depth = int(resp.get("Attributes", {}).get("ApproximateNumberOfMessages", 0))
                    if depth > 0:
                        await logger.awarning("SQS DLQ has active messages", depth=depth)
                        anomaly_event = Event(
                            event_type="anomaly",
                            resource_id="dlq",
                            state="FAILED",
                            data={
                                "message": f"DLQ '{settings.sqs_dlq_url}' has {depth} message(s) pending investigation.",
                                "dlq_depth": depth,
                                "queue_url": settings.sqs_dlq_url,
                            },
                        )
                        await event_bus.publish(anomaly_event)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                await logger.aerror("Error polling SQS DLQ depth", error=str(e))
    except asyncio.CancelledError:
        await logger.ainfo("DLQ poller background task stopped")


async def poll_edge_sync(
    session: aioboto3.Session,
    settings: Any,
    interval: float = 120.0,
) -> None:
    """Periodically execute desired-actual state synchronization against Sovereign."""
    from broker.services.dynamodb import DynamoDBService
    from broker.services.sovereign_client import SovereignClient
    from broker.services.sync_service import SovereignSyncService

    await logger.ainfo("Starting edge state synchronization background task", interval=interval)
    try:
        while True:
            await asyncio.sleep(interval)
            try:
                # Initialize connections
                sovereign = SovereignClient(settings)
                async with session.resource(
                    "dynamodb",
                    endpoint_url=settings.aws_endpoint_url,
                    region_name=settings.aws_region,
                ) as dynamodb_res:
                    db = DynamoDBService(dynamodb_res, settings)
                    sync_service = SovereignSyncService(db, sovereign)
                    await sync_service.sync_all_resources()
                await sovereign.close()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                await logger.aerror("Error during background edge state synchronization", error=str(e))
    except asyncio.CancelledError:
        await logger.ainfo("Edge state synchronization background task stopped")


# ---------------------------------------------------------------------------
# Application lifespan — startup / shutdown
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Manage application lifecycle: initialize shared resources on startup,
    clean up on shutdown."""
    settings = get_settings()

    await logger.ainfo("Starting Advanced AI Service Broker", version="0.1.0")

    # Create shared aioboto3 session
    session = aioboto3.Session(
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
        region_name=settings.aws_region,
    )
    app.state.aws_session = session
    app.state.settings = settings

    # Initialize event bus for SSE/WebSocket broadcasting
    event_bus = EventBus()
    app.state.event_bus = event_bus

    await logger.ainfo(
        "Infrastructure initialized",
        dynamodb_table=settings.dynamodb_table_name,
        sqs_queue=settings.sqs_queue_url,
    )

    # Start the SQS DLQ poller background task
    poller_task = asyncio.create_task(
        poll_dlq_depth(event_bus, session, settings, interval=60.0)
    )
    app.state.dlq_poller_task = poller_task

    # Start the Edge Sync poller background task
    sync_task = asyncio.create_task(
        poll_edge_sync(session, settings, interval=120.0)
    )
    app.state.edge_sync_task = sync_task

    # Start the CloudWatch metrics exporter background task
    cw_exporter = CloudWatchMetricsExporter(event_bus, session, settings, interval=60.0)
    await cw_exporter.start()
    app.state.cw_exporter = cw_exporter

    yield

    # Shutdown
    await logger.ainfo("Shutting down Advanced AI Service Broker")
    poller_task.cancel()
    sync_task.cancel()
    await cw_exporter.stop()
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.gather(poller_task, sync_task, return_exceptions=True)
    await event_bus.shutdown()


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------
def create_app() -> FastAPI:
    """Build and configure the FastAPI application."""
    settings = get_settings()

    app = FastAPI(
        title="Advanced AI Service Broker",
        description=(
            "AI-driven infrastructure provisioning with natural language intent parsing, "
            "predictive scaling, and event-driven real-time feedback."
        ),
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # -- Middleware (order matters: outermost first) --
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(StructuredLoggingMiddleware)
    app.add_middleware(RequestIdMiddleware)
    app.add_middleware(APIKeyMiddleware, api_keys=settings.api_keys)

    # -- Exception handlers --
    register_exception_handlers(app)

    # -- Routers --
    app.include_router(health.router)
    app.include_router(catalog.router)
    app.include_router(intent.router, prefix="/api/v1/intent", tags=["Intent Parsing"])
    app.include_router(resources.router, prefix="/api/v1/resources", tags=["Resources"])
    app.include_router(events.router, prefix="/api/v1/events", tags=["Events"])
    app.include_router(scaling.router, prefix="/api/v1/scaling", tags=["Scaling"])
    app.include_router(maintenance.router, prefix="/api/v1/maintenance", tags=["Maintenance"])

    return app


# Singleton application instance
app = create_app()


def run() -> None:
    """Entry point for `broker-api` script."""
    settings = get_settings()
    uvicorn.run(
        "broker.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=True,
        log_level=settings.log_level.lower(),
    )
