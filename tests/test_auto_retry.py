"""
Unit tests for the Auto-Retry Agent.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from broker.schemas.intent import (
    IntentAction,
    ParsedConfiguration,
    ValidationResult,
)
from broker.schemas.resource import ResourceRecord, ResourceState
from broker.services.auto_retry_agent import AutoRetryAgent


@pytest.fixture
def mock_db():
    db = AsyncMock()
    # Mock update_item in table
    mock_table = AsyncMock()
    db._get_table.return_value = mock_table
    return db


@pytest.fixture
def mock_sqs():
    return AsyncMock()


@pytest.fixture
def mock_llm():
    return AsyncMock()


@pytest.fixture
def mock_safety():
    return AsyncMock()


@pytest.fixture
def agent(mock_db, mock_sqs, mock_llm, mock_safety):
    return AutoRetryAgent(mock_db, mock_sqs, mock_llm, mock_safety)


class TestAutoRetryAgent:
    """Tests for the AutoRetryAgent service."""

    @pytest.mark.asyncio
    async def test_retry_skipped_if_not_failed(self, agent, mock_db):
        """Should skip auto-retry if resource state is not FAILED."""
        record = ResourceRecord(
            resource_id="res-123",
            resource_type="create_route",
            state=ResourceState.ACTIVE,
        )
        mock_db.get_resource.return_value = record

        res = await agent.auto_retry_resource("res-123")
        assert res["status"] == "skipped"
        assert "not FAILED" in res["message"]

    @pytest.mark.asyncio
    async def test_retry_failed_validation(self, agent, mock_db, mock_llm, mock_safety):
        """Should return validation failure if LLM corrected config violates safety policies."""
        record = ResourceRecord(
            resource_id="res-123",
            resource_type="create_route",
            state=ResourceState.FAILED,
            error_message="Missing route_name",
            configuration={"prefix": "/test"},
        )
        mock_db.get_resource.return_value = record

        corrected_config = ParsedConfiguration(
            action=IntentAction.CREATE_ROUTE,
            target_service="test-service",
            parameters={"prefix": "/test"},  # Still missing route_name
            reasoning="Attempting fix but still invalid",
        )
        mock_llm.parse_intent.return_value = corrected_config
        mock_safety.validate_config.return_value = ValidationResult(
            is_valid=False,
            errors=["Missing route_name parameter"],
        )

        res = await agent.auto_retry_resource("res-123")
        assert res["status"] == "failed_validation"
        assert "Missing route_name parameter" in res["errors"]

    @pytest.mark.asyncio
    async def test_retry_successful_flow(self, agent, mock_db, mock_llm, mock_safety, mock_sqs):
        """Should update DB config to corrected config, set state to PENDING, and enqueue task on success."""
        record = ResourceRecord(
            resource_id="res-123",
            resource_type="create_route",
            state=ResourceState.FAILED,
            error_message="Missing route_name",
            configuration={"prefix": "/test"},
        )
        mock_db.get_resource.return_value = record

        corrected_config = ParsedConfiguration(
            action=IntentAction.CREATE_ROUTE,
            target_service="test-service",
            parameters={"route_name": "test-route", "prefix": "/test", "target_cluster": "test-cluster"},
            reasoning="Added route_name and target_cluster",
        )
        mock_llm.parse_intent.return_value = corrected_config
        mock_safety.validate_config.return_value = ValidationResult(is_valid=True)
        mock_sqs.enqueue_task.return_value = "msg-999"

        res = await agent.auto_retry_resource("res-123", correlation_id="corr-999")
        assert res["status"] == "retrying"
        assert res["message_id"] == "msg-999"
        assert res["diagnosed_config"]["parameters"]["route_name"] == "test-route"

        # Verify DB update item was called
        table = mock_db._get_table.return_value
        table.update_item.assert_called_once()
        # Verify SQS task enqueued
        mock_sqs.enqueue_task.assert_called_once()
        task = mock_sqs.enqueue_task.call_args[0][0]
        assert task.correlation_id == "corr-999"
        assert task.resource_id == "res-123"


def test_auto_retry_api_endpoint():
    from unittest.mock import AsyncMock

    from fastapi.testclient import TestClient

    from broker.main import app

    # Mock the dependencies
    mock_db = AsyncMock()
    # Mock update_item inside table
    mock_table = AsyncMock()
    mock_db._get_table.return_value = mock_table

    mock_sqs = AsyncMock()
    mock_llm = AsyncMock()
    mock_safety = AsyncMock()

    # Stub the failed resource record
    record = ResourceRecord(
        resource_id="res-123",
        resource_type="create_route",
        state=ResourceState.FAILED,
        error_message="Fail",
        configuration={"prefix": "/test"},
    )
    mock_db.get_resource.return_value = record

    corrected_config = ParsedConfiguration(
        action=IntentAction.CREATE_ROUTE,
        target_service="test-service",
        parameters={"route_name": "test-route", "prefix": "/test", "target_cluster": "test-cluster"},
        reasoning="Fixing",
    )
    mock_llm.parse_intent.return_value = corrected_config
    mock_safety.validate_config.return_value = ValidationResult(is_valid=True)
    mock_sqs.enqueue_task.return_value = "msg-api-999"

    # Override dependencies
    from broker.dependencies import (
        get_dynamodb_service,
        get_llm_gateway,
        get_safety_service,
        get_sqs_service,
    )

    async def override_db():
        yield mock_db

    async def override_sqs():
        yield mock_sqs

    def override_llm():
        return mock_llm

    async def override_safety():
        return mock_safety

    app.dependency_overrides[get_dynamodb_service] = override_db
    app.dependency_overrides[get_sqs_service] = override_sqs
    app.dependency_overrides[get_llm_gateway] = override_llm
    app.dependency_overrides[get_safety_service] = override_safety

    try:
        with TestClient(app) as client:
            response = client.post("/api/v1/resources/res-123/auto-retry")
            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "retrying"
            assert data["message_id"] == "msg-api-999"
    finally:
        app.dependency_overrides.clear()

