"""
Unit tests for the Sovereign Edge Sync Service.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from broker.schemas.resource import ResourceRecord, ResourceState
from broker.services.sync_service import SovereignSyncService


@pytest.fixture
def mock_dynamodb_service():
    """Mock DynamoDBService."""
    db = AsyncMock()
    # Mock _get_table update_item
    mock_table = AsyncMock()
    db._get_table.return_value = mock_table
    return db


@pytest.fixture
def mock_sovereign_client():
    """Mock SovereignClient."""
    return AsyncMock()


@pytest.fixture
def sync_service(mock_dynamodb_service, mock_sovereign_client):
    """SovereignSyncService fixture."""
    return SovereignSyncService(mock_dynamodb_service, mock_sovereign_client)


class TestSovereignSyncService:
    """Tests for SovereignSyncService."""

    @pytest.mark.asyncio
    async def test_sync_all_resources_empty(self, sync_service, mock_dynamodb_service):
        """If there are no active resources, sync_all_resources should return empty list."""
        mock_dynamodb_service.list_resources.return_value = []
        res = await sync_service.sync_all_resources()
        assert res == []
        mock_dynamodb_service.list_resources.assert_called_once_with(state=ResourceState.ACTIVE)

    @pytest.mark.asyncio
    async def test_sync_resource_in_sync(self, sync_service, mock_dynamodb_service, mock_sovereign_client):
        """If Sovereign config matches DynamoDB record, sync_status should be IN_SYNC."""
        record = ResourceRecord(
            resource_id="res-123",
            resource_type="create_route",
            state=ResourceState.ACTIVE,
            configuration={
                "action": "create_route",
                "parameters": {
                    "route_name": "test-route",
                    "prefix": "/test",
                    "target_cluster": "test-cluster",
                }
            },
            sync_status=None,
            actual_state=None,
        )

        mock_sovereign_client.get_route.return_value = {
            "route_name": "test-route",
            "match": {"prefix": "/test", "headers": {}, "query_parameters": {}},
            "target_cluster": "test-cluster",
            "weighted_clusters": None,
            "timeout_ms": 15000,
            "retry_on": None,
            "max_retries": 1,
            "metadata": {},
        }

        updated = await sync_service.sync_resource(record)

        assert updated is not None
        assert updated.sync_status == "IN_SYNC"
        assert updated.actual_state == ResourceState.ACTIVE

        # Verify DynamoDB update was triggered
        mock_dynamodb_service._get_table.assert_called_once()
        table = mock_dynamodb_service._get_table.return_value
        table.update_item.assert_called_once()

    @pytest.mark.asyncio
    async def test_sync_resource_drifted(self, sync_service, mock_dynamodb_service, mock_sovereign_client):
        """If Sovereign config differs from DynamoDB, sync_status should be DRIFTED."""
        record = ResourceRecord(
            resource_id="res-123",
            resource_type="create_route",
            state=ResourceState.ACTIVE,
            configuration={
                "action": "create_route",
                "parameters": {
                    "route_name": "test-route",
                    "prefix": "/test",
                    "target_cluster": "test-cluster",
                }
            },
            sync_status="IN_SYNC",
            actual_state=ResourceState.ACTIVE,
        )

        # Sovereign returns a different cluster (drift!)
        mock_sovereign_client.get_route.return_value = {
            "route_name": "test-route",
            "match": {"prefix": "/test", "headers": None, "query_parameters": None},
            "target_cluster": "drifted-cluster",
            "weighted_clusters": None,
            "timeout_ms": 15000,
            "retry_policy": None,
        }

        updated = await sync_service.sync_resource(record)

        assert updated is not None
        assert updated.sync_status == "DRIFTED"
        assert updated.actual_state == ResourceState.ACTIVE

    @pytest.mark.asyncio
    async def test_sync_resource_absent(self, sync_service, mock_dynamodb_service, mock_sovereign_client):
        """If Sovereign cannot return config (e.g. throws exception), status should be ABSENT."""
        record = ResourceRecord(
            resource_id="res-123",
            resource_type="create_route",
            state=ResourceState.ACTIVE,
            configuration={
                "action": "create_route",
                "parameters": {
                    "route_name": "test-route",
                    "prefix": "/test",
                    "target_cluster": "test-cluster",
                }
            },
            sync_status="IN_SYNC",
            actual_state=ResourceState.ACTIVE,
        )

        mock_sovereign_client.get_route.side_effect = Exception("Not found")

        updated = await sync_service.sync_resource(record)

        assert updated is not None
        assert updated.sync_status == "ABSENT"
        assert updated.actual_state == ResourceState.DELETED
