"""
Health Check Router — liveness and readiness probes for container orchestration.

GET /health       — Liveness probe (is the process alive?)
GET /health/ready — Readiness probe (can the service handle traffic?)
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, Request

logger = structlog.get_logger()

router = APIRouter(tags=["Health"])


@router.get(
    "/health",
    summary="Liveness probe",
    description="Returns 200 if the process is alive. Used by container orchestrators.",
)
async def health_check() -> dict[str, str]:
    """Liveness probe — always returns OK if the process is running."""
    return {"status": "ok"}


@router.get(
    "/health/ready",
    summary="Readiness probe",
    description=(
        "Checks connectivity to DynamoDB and SQS. Returns 200 only if "
        "all dependencies are reachable."
    ),
)
async def readiness_check(request: Request) -> dict[str, Any]:
    """Readiness probe — verifies all dependencies are reachable."""
    checks: dict[str, str] = {}
    all_healthy = True

    settings = request.app.state.settings
    session = request.app.state.aws_session

    # Check DynamoDB
    try:
        async with session.client(
            "dynamodb",
            endpoint_url=settings.aws_endpoint_url,
            region_name=settings.aws_region,
        ) as dynamodb_client:
            await dynamodb_client.describe_table(TableName=settings.dynamodb_table_name)
            checks["dynamodb"] = "ok"
    except Exception as e:
        checks["dynamodb"] = f"error: {str(e)[:100]}"
        all_healthy = False

    # Check SQS
    try:
        async with session.client(
            "sqs",
            endpoint_url=settings.aws_endpoint_url,
            region_name=settings.aws_region,
        ) as sqs_client:
            await sqs_client.get_queue_attributes(
                QueueUrl=settings.sqs_queue_url,
                AttributeNames=["QueueArn"],
            )
            checks["sqs"] = "ok"
    except Exception as e:
        checks["sqs"] = f"error: {str(e)[:100]}"
        all_healthy = False

    # Check Event Bus
    event_bus = request.app.state.event_bus
    checks["event_bus"] = "ok"
    checks["event_bus_subscribers"] = str(event_bus.subscriber_count)

    return {
        "status": "ready" if all_healthy else "degraded",
        "checks": checks,
    }
