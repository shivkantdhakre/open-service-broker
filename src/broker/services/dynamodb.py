"""
DynamoDB Service — async CRUD operations for resource state management.

Uses aioboto3 for non-blocking I/O and ConditionExpression for optimistic
concurrency control. All state transitions are validated against the
resource state machine.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog
from boto3.dynamodb.conditions import Attr, Key  # type: ignore[import-untyped]

from broker.config import Settings
from broker.schemas.resource import VALID_TRANSITIONS, ResourceRecord, ResourceState

logger = structlog.get_logger()


class OptimisticLockError(Exception):
    """Raised when a concurrent update conflicts with the expected version."""

    pass


class InvalidStateTransitionError(Exception):
    """Raised when a resource state transition is not valid."""

    pass


class ResourceNotFoundError(Exception):
    """Raised when a resource is not found in DynamoDB."""

    pass


class DynamoDBService:
    """Async DynamoDB operations for resource state management."""

    def __init__(self, dynamodb_resource: Any, settings: Settings) -> None:
        self._dynamodb = dynamodb_resource
        self._settings = settings
        self._table_name = settings.dynamodb_table_name

    async def _get_table(self) -> Any:
        """Get the DynamoDB table resource."""
        return await self._dynamodb.Table(self._table_name)

    async def create_resource(self, record: ResourceRecord) -> ResourceRecord:
        """Create a new resource record in DynamoDB.

        Args:
            record: The resource record to create.

        Returns:
            The created resource record.

        Raises:
            Exception: If a resource with the same ID already exists.
        """
        table = await self._get_table()

        await table.put_item(
            Item=record.to_dynamodb_item(),
            ConditionExpression=Attr("resource_id").not_exists(),
        )

        await logger.ainfo(
            "Resource created",
            resource_id=record.resource_id,
            resource_type=record.resource_type,
            state=record.state,
        )

        return record

    async def get_resource(self, resource_id: str, resource_type: str | None = None) -> ResourceRecord | None:
        """Get a resource record by ID.

        Args:
            resource_id: The resource's partition key.
            resource_type: Optional sort key for exact lookup.

        Returns:
            The resource record, or None if not found.
        """
        table = await self._get_table()

        if resource_type:
            response = await table.get_item(
                Key={"resource_id": resource_id, "resource_type": resource_type}
            )
            item = response.get("Item")
            if item:
                return ResourceRecord.from_dynamodb_item(item)
            return None

        # Query by partition key only (returns first match)
        response = await table.query(
            KeyConditionExpression=Key("resource_id").eq(resource_id),
            Limit=1,
        )
        items = response.get("Items", [])
        if items:
            return ResourceRecord.from_dynamodb_item(items[0])
        return None

    async def update_state(
        self,
        resource_id: str,
        resource_type: str,
        new_state: ResourceState,
        expected_version: int,
        error_message: str | None = None,
    ) -> ResourceRecord:
        """Update a resource's state with optimistic concurrency control.

        Args:
            resource_id: The resource ID.
            resource_type: The resource type (sort key).
            new_state: The target state.
            expected_version: The version number expected (for optimistic locking).
            error_message: Optional error message (for FAILED state).

        Returns:
            The updated resource record.

        Raises:
            OptimisticLockError: If the version doesn't match.
            InvalidStateTransitionError: If the transition is not valid.
            ResourceNotFoundError: If the resource doesn't exist.
        """
        # Fetch current record to validate transition
        current = await self.get_resource(resource_id, resource_type)
        if current is None:
            raise ResourceNotFoundError(f"Resource {resource_id} not found")

        if not current.can_transition_to(new_state):
            raise InvalidStateTransitionError(
                f"Cannot transition from {current.state} to {new_state}. "
                f"Valid transitions: {VALID_TRANSITIONS.get(current.state, set())}"
            )

        table = await self._get_table()
        now = datetime.now(UTC)

        update_expr = (
            "SET #state = :new_state, "
            "updated_at = :updated_at, "
            "version = :new_version"
        )
        expr_values: dict[str, Any] = {
            ":new_state": new_state.value,
            ":updated_at": now.isoformat(),
            ":new_version": expected_version + 1,
        }
        expr_names = {"#state": "state"}

        if error_message is not None:
            update_expr += ", error_message = :error_message"
            expr_values[":error_message"] = error_message

        try:
            response = await table.update_item(
                Key={"resource_id": resource_id, "resource_type": resource_type},
                UpdateExpression=update_expr,
                ConditionExpression=Attr("version").eq(expected_version),
                ExpressionAttributeValues=expr_values,
                ExpressionAttributeNames=expr_names,
                ReturnValues="ALL_NEW",
            )
        except Exception as e:
            if "ConditionalCheckFailedException" in str(type(e).__name__):
                raise OptimisticLockError(
                    f"Resource {resource_id} was modified concurrently "
                    f"(expected version {expected_version})"
                ) from e
            raise

        await logger.ainfo(
            "Resource state updated",
            resource_id=resource_id,
            old_state=current.state,
            new_state=new_state,
            version=expected_version + 1,
        )

        return ResourceRecord.from_dynamodb_item(response["Attributes"])

    async def list_resources(
        self,
        resource_type: str | None = None,
        state: ResourceState | None = None,
        limit: int = 100,
    ) -> list[ResourceRecord]:
        """List resources with optional filtering.

        Args:
            resource_type: Filter by resource type.
            state: Filter by state.
            limit: Maximum number of results.

        Returns:
            List of matching resource records.
        """
        table = await self._get_table()

        # Build filter expression
        filter_parts: list[Any] = []
        if state:
            filter_parts.append(Attr("state").eq(state.value))
        if resource_type:
            filter_parts.append(Attr("resource_type").eq(resource_type))

        scan_kwargs: dict[str, Any] = {"Limit": limit}
        if filter_parts:
            combined_filter = filter_parts[0]
            for f in filter_parts[1:]:
                combined_filter = combined_filter & f
            scan_kwargs["FilterExpression"] = combined_filter

        response = await table.scan(**scan_kwargs)
        items = response.get("Items", [])

        return [ResourceRecord.from_dynamodb_item(item) for item in items]

    async def delete_resource(self, resource_id: str, resource_type: str) -> None:
        """Soft-delete a resource by transitioning to DELETED state.

        This does not physically remove the record — it marks it as deleted
        for audit purposes.
        """
        current = await self.get_resource(resource_id, resource_type)
        if current is None:
            raise ResourceNotFoundError(f"Resource {resource_id} not found")

        await self.update_state(
            resource_id=resource_id,
            resource_type=resource_type,
            new_state=ResourceState.DELETED,
            expected_version=current.version,
        )
