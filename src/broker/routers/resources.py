"""
Resources Router — API endpoints for resource lifecycle management.

GET    /           — List all resources with optional filters
GET    /{id}       — Get a single resource by ID
DELETE /{id}       — Initiate deprovisioning of a resource
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, HTTPException, Query, Request, status

from broker.dependencies import DynamoDBDep, EventBusDep, SQSDep
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
    state: ResourceState | None = Query(default=None, description="Filter by state"),
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
