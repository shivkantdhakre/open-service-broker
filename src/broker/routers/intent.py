"""
Intent Parsing Router — API endpoints for natural language → configuration.

POST /parse  — Translate natural language into a validated configuration
POST /apply  — Queue a validated configuration for provisioning
GET  /history — Audit trail of past intent translations
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, HTTPException, status

from broker.dependencies import DynamoDBDep, EventBusDep, LLMDep, SafetyDep, SQSDep
from broker.schemas.intent import (
    IntentApplyRequest,
    IntentHistoryItem,
    IntentRequest,
    IntentResponse,
)
from broker.schemas.resource import ResourceRecord, ResourceState
from broker.schemas.task import TaskMessage, TaskType
from broker.services.intent_parser import IntentParserService
from broker.services.llm_gateway import LLMParsingError
from broker.services.event_bus import Event

logger = structlog.get_logger()

router = APIRouter()


@router.post(
    "/parse",
    response_model=IntentResponse,
    status_code=status.HTTP_200_OK,
    summary="Parse natural language into infrastructure configuration",
    description=(
        "Translates a developer's plain English request into a structured "
        "Envoy/Sovereign configuration. The result includes validation status "
        "and blast radius analysis."
    ),
)
async def parse_intent(
    request: IntentRequest,
    llm: LLMDep,
    safety: SafetyDep,
    event_bus: EventBusDep,
) -> IntentResponse:
    """Parse a natural language request into a validated configuration."""
    parser = IntentParserService(llm, safety)

    try:
        response = await parser.parse_and_validate(
            natural_language=request.natural_language,
            context=request.context,
        )
        # Publish success event
        await event_bus.publish(
            Event(
                event_type="intent_parsed",
                resource_id=response.request_id,
                data={
                    "status": "success",
                    "action": response.parsed_configuration.action.value if hasattr(response.parsed_configuration.action, "value") else str(response.parsed_configuration.action),
                },
            )
        )
    except LLMParsingError as e:
        await logger.aerror("Intent parsing failed", error=str(e))
        # Publish failure event
        await event_bus.publish(
            Event(
                event_type="intent_parsed",
                data={"status": "failed", "error": str(e)},
            )
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "type": "intent_parsing_error",
                "title": "Failed to parse intent",
                "detail": str(e),
                "raw_output": e.raw_output,
            },
        ) from e

    # If validation failed, still return the response but with 200
    # so the client can see what went wrong and retry
    if not response.validation.is_valid:
        await logger.awarning(
            "Configuration validation failed",
            request_id=response.request_id,
            errors=response.validation.errors,
        )

    return response


@router.post(
    "/apply",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Apply a validated configuration",
    description=(
        "Accepts a previously parsed and validated configuration, creates a "
        "resource record, and queues it for asynchronous provisioning."
    ),
)
async def apply_intent(
    request: IntentApplyRequest,
    dynamodb: DynamoDBDep,
    sqs: SQSDep,
    event_bus: EventBusDep,
    safety: SafetyDep,
) -> dict[str, str]:
    """Queue a validated configuration for provisioning."""
    config = request.parsed_configuration

    # Re-validate before applying (defense in depth)
    validation = await safety.validate_config(config, None)
    if not validation.is_valid and not request.force:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "type": "validation_error",
                "title": "Configuration failed re-validation",
                "errors": validation.errors,
            },
        )

    # Check blast radius
    blast_radius = await safety.simulate_blast_radius(config)
    if not blast_radius.is_safe and not request.force:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "type": "blast_radius_exceeded",
                "title": "Configuration change has unacceptable blast radius",
                "risk_score": blast_radius.risk_score,
                "affected_services": blast_radius.affected_services,
                "description": blast_radius.description,
            },
        )

    # Create resource record in PENDING state
    now = datetime.now(UTC)
    resource = ResourceRecord(
        resource_id=request.request_id,
        resource_type=config.action.value,
        state=ResourceState.PENDING,
        configuration=config.parameters,
        created_by="api",
        created_at=now,
        updated_at=now,
    )
    await dynamodb.create_resource(resource)

    # Enqueue task for background processing
    task = TaskMessage(
        task_id=request.request_id,
        task_type=TaskType.PROVISION,
        resource_id=request.request_id,
        configuration=config.model_dump(),
        requested_by="api",
    )
    message_id = await sqs.enqueue_task(task)

    await logger.ainfo(
        "Configuration queued for provisioning",
        request_id=request.request_id,
        message_id=message_id,
        action=config.action,
        target_service=config.target_service,
    )

    return {
        "status": "accepted",
        "request_id": request.request_id,
        "resource_id": request.request_id,
        "message": "Configuration has been queued for provisioning. "
        "Subscribe to /api/v1/events/stream for real-time updates.",
    }


@router.get(
    "/history",
    response_model=list[IntentHistoryItem],
    summary="Get intent translation history",
    description="Returns an audit trail of past intent translations.",
)
async def get_intent_history(
    dynamodb: DynamoDBDep,
    limit: int = 50,
) -> list[IntentHistoryItem]:
    """Return past intent translations for audit purposes."""
    resources = await dynamodb.list_resources(resource_type=None, state=None, limit=limit)

    history: list[IntentHistoryItem] = []
    for resource in resources:
        history.append(
            IntentHistoryItem(
                request_id=resource.resource_id,
                original_input=resource.metadata.get("original_input", ""),
                action=resource.resource_type,  # type: ignore[arg-type]
                target_service=resource.metadata.get("target_service", ""),
                status=resource.state.value,
                created_at=resource.created_at,
                applied_at=resource.updated_at if resource.state == ResourceState.ACTIVE else None,
            )
        )

    return history
