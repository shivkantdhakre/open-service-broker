"""
Integration tests for the DynamoDB Service.

Verifies:
- Optimistic locking chain (PENDING -> PROVISIONING -> ACTIVE) and version increments.
- Concurrent write conflicts and rejection via OptimisticLockError.
- Invalid state transitions rejection via InvalidStateTransitionError.
- CRUD operations (create, get, list, soft delete).
"""

from __future__ import annotations

import pytest

from broker.schemas.resource import ResourceRecord, ResourceState
from broker.services.dynamodb import (
    InvalidStateTransitionError,
    OptimisticLockError,
    ResourceNotFoundError,
)


@pytest.mark.asyncio
class TestDynamoDBIntegration:
    """End-to-end integration tests for DynamoDBService."""

    async def test_resource_lifecycle_and_version_chain(self, async_dynamodb_service):
        """Verify the happy path lifecycle of a resource and version increments."""
        # 1. Create resource in PENDING state
        record = ResourceRecord(
            resource_id="res-001",
            resource_type="create_route",
            state=ResourceState.PENDING,
            configuration={"route_name": "test-route", "prefix": "/api"},
            created_by="test-user",
        )
        created = await async_dynamodb_service.create_resource(record)
        assert created.resource_id == "res-001"
        assert created.state == ResourceState.PENDING
        assert created.version == 1

        # Retrieve and verify
        retrieved = await async_dynamodb_service.get_resource("res-001", "create_route")
        assert retrieved is not None
        assert retrieved.state == ResourceState.PENDING
        assert retrieved.version == 1

        # 2. Transition PENDING -> PROVISIONING
        updated = await async_dynamodb_service.update_state(
            resource_id="res-001",
            resource_type="create_route",
            new_state=ResourceState.PROVISIONING,
            expected_version=1,
        )
        assert updated.state == ResourceState.PROVISIONING
        assert updated.version == 2

        # 3. Transition PROVISIONING -> ACTIVE
        active = await async_dynamodb_service.update_state(
            resource_id="res-001",
            resource_type="create_route",
            new_state=ResourceState.ACTIVE,
            expected_version=2,
        )
        assert active.state == ResourceState.ACTIVE
        assert active.version == 3

        # Retrieve and verify terminal state
        final = await async_dynamodb_service.get_resource("res-001", "create_route")
        assert final.state == ResourceState.ACTIVE
        assert final.version == 3

    async def test_concurrent_write_conflict(self, async_dynamodb_service):
        """Verify that concurrent updates using a stale version are rejected."""
        record = ResourceRecord(
            resource_id="res-002",
            resource_type="create_route",
            state=ResourceState.PENDING,
        )
        await async_dynamodb_service.create_resource(record)

        # Retrieve two copies of the same record (both at version 1)
        writer1 = await async_dynamodb_service.get_resource("res-002", "create_route")
        writer2 = await async_dynamodb_service.get_resource("res-002", "create_route")

        assert writer1.version == 1
        assert writer2.version == 1

        # Writer 1 updates state successfully
        await async_dynamodb_service.update_state(
            resource_id="res-002",
            resource_type="create_route",
            new_state=ResourceState.PROVISIONING,
            expected_version=writer1.version,
        )

        # Writer 2 attempts to update using the stale version (expected_version=1)
        # We transition to FAILED because it's valid from both PENDING and PROVISIONING,
        # ensuring we trigger the OptimisticLockError instead of an InvalidStateTransitionError.
        with pytest.raises(OptimisticLockError) as exc_info:
            await async_dynamodb_service.update_state(
                resource_id="res-002",
                resource_type="create_route",
                new_state=ResourceState.FAILED,
                expected_version=writer2.version,
            )
        assert "modified concurrently" in str(exc_info.value)

    async def test_invalid_state_transition(self, async_dynamodb_service):
        """Verify that invalid state transitions (e.g. PENDING -> ACTIVE) are rejected."""
        record = ResourceRecord(
            resource_id="res-003",
            resource_type="create_route",
            state=ResourceState.PENDING,
        )
        await async_dynamodb_service.create_resource(record)

        # Retrieve and attempt invalid transition (PENDING -> ACTIVE)
        retrieved = await async_dynamodb_service.get_resource("res-003", "create_route")
        with pytest.raises(InvalidStateTransitionError) as exc_info:
            await async_dynamodb_service.update_state(
                resource_id="res-003",
                resource_type="create_route",
                new_state=ResourceState.ACTIVE,
                expected_version=retrieved.version,
            )
        assert "Cannot transition from PENDING to ACTIVE" in str(exc_info.value)

    async def test_get_nonexistent_resource(self, async_dynamodb_service):
        """Verify that looking up a nonexistent resource returns None."""
        res = await async_dynamodb_service.get_resource("nonexistent", "type")
        assert res is None

    async def test_list_resources_with_filtering(self, async_dynamodb_service):
        """Verify listing resources and filtering by state and type."""
        # Create multiple resources
        res1 = ResourceRecord(
            resource_id="res-list-1",
            resource_type="create_route",
            state=ResourceState.ACTIVE,
        )
        res2 = ResourceRecord(
            resource_id="res-list-2",
            resource_type="create_route",
            state=ResourceState.PENDING,
        )
        res3 = ResourceRecord(
            resource_id="res-list-3",
            resource_type="update_rate_limit",
            state=ResourceState.ACTIVE,
        )

        await async_dynamodb_service.create_resource(res1)
        await async_dynamodb_service.create_resource(res2)
        await async_dynamodb_service.create_resource(res3)

        # List all
        all_res = await async_dynamodb_service.list_resources()
        assert len(all_res) >= 3

        # Filter by state
        active_res = await async_dynamodb_service.list_resources(state=ResourceState.ACTIVE)
        assert any(r.resource_id == "res-list-1" for r in active_res)
        assert any(r.resource_id == "res-list-3" for r in active_res)
        assert not any(r.resource_id == "res-list-2" for r in active_res)

        # Filter by type
        route_res = await async_dynamodb_service.list_resources(resource_type="create_route")
        assert any(r.resource_id == "res-list-1" for r in route_res)
        assert any(r.resource_id == "res-list-2" for r in route_res)
        assert not any(r.resource_id == "res-list-3" for r in route_res)

    async def test_soft_delete_resource(self, async_dynamodb_service):
        """Verify soft-deleting a resource transitions its state to DELETED."""
        record = ResourceRecord(
            resource_id="res-delete-1",
            resource_type="create_route",
            state=ResourceState.DEPROVISIONING,
        )
        await async_dynamodb_service.create_resource(record)

        # Delete it
        await async_dynamodb_service.delete_resource("res-delete-1", "create_route")

        # Verify state is DELETED
        deleted = await async_dynamodb_service.get_resource("res-delete-1", "create_route")
        assert deleted.state == ResourceState.DELETED
        assert deleted.version == 2

        # Verify delete on nonexistent raises ResourceNotFoundError
        with pytest.raises(ResourceNotFoundError):
            await async_dynamodb_service.delete_resource("nonexistent", "type")
