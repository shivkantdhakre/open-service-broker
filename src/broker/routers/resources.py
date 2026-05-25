"""
Resources Router — API endpoints for resource lifecycle management.

GET    /           — List all resources with optional filters
GET    /{id}       — Get a single resource by ID
DELETE /{id}       — Initiate deprovisioning of a resource
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, Query, Request, status

from broker.dependencies import DynamoDBDep, EventBusDep, LLMDep, SafetyDep, SQSDep  # noqa: TC001
from broker.schemas.resource import ResourceRecord, ResourceState
from broker.schemas.task import TaskMessage, TaskType
from broker.services.dynamodb import ResourceNotFoundError
from broker.services.sovereign_client import SovereignClient
from broker.services.sync_service import SovereignSyncService

logger = structlog.get_logger()

router = APIRouter()


@router.post(
    "/sync",
    response_model=list[ResourceRecord],
    summary="Trigger manual desired-actual state synchronization",
)
async def manual_sync_resources(
    dynamodb: DynamoDBDep,
    request: Request,
) -> list[ResourceRecord]:
    """Manually trigger edge proxy configuration sync and detect drift."""
    settings = request.app.state.settings
    sovereign = SovereignClient(settings)
    try:
        sync_service = SovereignSyncService(dynamodb, sovereign)
        updated = await sync_service.sync_all_resources()
        return updated
    finally:
        await sovereign.close()


@router.get(
    "",
    response_model=list[ResourceRecord],
    summary="List all managed resources",
)
async def list_resources(
    dynamodb: DynamoDBDep,
    resource_type: str | None = Query(default=None, description="Filter by resource type"),
    state: ResourceState | None = Query(default=None, description="Filter by state"),  # noqa: B008
    limit: int = Query(default=100, ge=1, le=500),
) -> list[ResourceRecord]:
    """List resources with optional filtering by type and state."""
    return await dynamodb.list_resources(
        resource_type=resource_type,
        state=state,
        limit=limit,
    )


@router.get(
    "/{resource_id}",
    response_model=ResourceRecord,
    summary="Get resource details",
)
async def get_resource(
    resource_id: str,
    dynamodb: DynamoDBDep,
) -> ResourceRecord:
    """Get a single resource by its ID."""
    resource = await dynamodb.get_resource(resource_id)
    if resource is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "type": "resource_not_found",
                "title": "Resource not found",
                "detail": f"No resource with ID '{resource_id}' exists.",
            },
        )
    return resource


@router.delete(
    "/{resource_id}",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Decommission a resource",
)
async def delete_resource(
    resource_id: str,
    dynamodb: DynamoDBDep,
    sqs: SQSDep,
    event_bus: EventBusDep,
) -> dict[str, str]:
    """Initiate deprovisioning of a resource.

    Transitions the resource to DEPROVISIONING state and queues
    a deprovision task for the background worker.
    """
    resource = await dynamodb.get_resource(resource_id)
    if resource is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "type": "resource_not_found",
                "title": "Resource not found",
                "detail": f"No resource with ID '{resource_id}' exists.",
            },
        )

    if resource.state == ResourceState.DELETED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "type": "already_deleted",
                "title": "Resource already deleted",
                "detail": f"Resource '{resource_id}' is already in DELETED state.",
            },
        )

    try:
        await dynamodb.update_state(
            resource_id=resource.resource_id,
            resource_type=resource.resource_type,
            new_state=ResourceState.DEPROVISIONING,
            expected_version=resource.version,
        )
    except ResourceNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"type": "resource_not_found", "title": "Resource not found"},
        ) from None

    # Queue deprovision task
    task = TaskMessage(
        task_id=resource_id,
        task_type=TaskType.DEPROVISION,
        resource_id=resource_id,
        configuration=resource.configuration,
        requested_by="api",
    )
    await sqs.enqueue_task(task)

    return {
        "status": "accepted",
        "resource_id": resource_id,
        "message": "Resource deprovisioning has been initiated.",
    }


@router.post(
    "/{resource_id}/auto-retry",
    status_code=status.HTTP_200_OK,
    summary="Trigger auto-retry for a failed resource configuration",
)
async def auto_retry_resource(
    resource_id: str,
    dynamodb: DynamoDBDep,
    sqs: SQSDep,
    llm: LLMDep,
    safety: SafetyDep,
    request: Request,
) -> dict[str, Any]:
    """Analyze a failed configuration, diagnose using LLM, and trigger retry."""
    from broker.services.auto_retry_agent import AutoRetryAgent

    correlation_id = getattr(request.state, "request_id", None)
    agent = AutoRetryAgent(dynamodb, sqs, llm, safety)

    try:
        result = await agent.auto_retry_resource(resource_id, correlation_id)
        if result.get("status") == "failed_validation":
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "type": "auto_retry_validation_failed",
                    "title": "LLM fix failed validation checks",
                    "errors": result.get("errors"),
                    "diagnosed_config": result.get("diagnosed_config"),
                }
            )
        return result
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "type": "resource_not_found",
                "title": "Resource not found or invalid",
                "detail": str(e),
            }
        ) from e
