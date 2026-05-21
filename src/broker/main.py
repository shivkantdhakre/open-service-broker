"""
FastAPI application factory and lifespan management.

Initializes AWS clients, event bus, and background tasks on startup.
Mounts all routers and middleware.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import aioboto3
import structlog
import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from broker.config import get_settings
from broker.middleware.error_handler import register_exception_handlers
from broker.middleware.logging import StructuredLoggingMiddleware
from broker.middleware.request_id import RequestIdMiddleware
from broker.routers import events, health, intent, maintenance, resources, scaling
from broker.services.event_bus import EventBus

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

    yield

    # Shutdown
    await logger.ainfo("Shutting down Advanced AI Service Broker")
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

    # -- Exception handlers --
    register_exception_handlers(app)

    # -- Routers --
    app.include_router(health.router)
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
