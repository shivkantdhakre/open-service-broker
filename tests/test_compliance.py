"""
Unit and integration tests for OPA/Rego compliance checking.
"""

from __future__ import annotations

import httpx
import pytest
from unittest.mock import AsyncMock, patch

from broker.schemas.intent import IntentAction, ParsedConfiguration
from broker.services.opa_client import OPAClient
from broker.services.safety import SafetyService


@pytest.fixture
def mock_settings(settings):
    """Fixture to provide settings with OPA enabled."""
    settings.opa_enabled = True
    settings.opa_url = "http://localhost:8181"
    return settings


@pytest.fixture
def safety_service_with_opa(mock_settings):
    """Fixture to provide a SafetyService instance with OPA enabled."""
    mock_db = AsyncMock()
    return SafetyService(mock_db, mock_settings)


class TestOPAClient:
    """Tests for the OPAClient."""

    @pytest.mark.asyncio
    async def test_evaluate_policy_allowed(self):
        client = OPAClient("http://localhost:8181")
        mock_response = {
            "result": {
                "allow": True,
                "errors_list": [],
            }
        }

        with patch("httpx.AsyncClient.post") as mock_post:
            mock_post.return_value = AsyncMock(
                status_code=200,
                json=lambda: mock_response,
                raise_for_status=lambda: None,
            )
            res = await client.evaluate_policy(
                action="create_route",
                parameters={"route_name": "test"},
                blast_radius={"risk_score": 0.2},
            )
            assert res["is_valid"]
            assert not res["errors"]

    @pytest.mark.asyncio
    async def test_evaluate_policy_denied(self):
        client = OPAClient("http://localhost:8181")
        mock_response = {
            "result": {
                "allow": False,
                "errors_list": ["Action is not allowed by policy."],
            }
        }

        with patch("httpx.AsyncClient.post") as mock_post:
            mock_post.return_value = AsyncMock(
                status_code=200,
                json=lambda: mock_response,
                raise_for_status=lambda: None,
            )
            res = await client.evaluate_policy(
                action="create_route",
                parameters={"route_name": "test"},
                blast_radius={"risk_score": 0.2},
            )
            assert not res["is_valid"]
            assert "Action is not allowed by policy." in res["errors"]

    @pytest.mark.asyncio
    async def test_evaluate_policy_unreachable_dev_fallback(self):
        client = OPAClient("http://localhost:8181")

        with patch("httpx.AsyncClient.post", side_effect=httpx.ConnectError("Connection refused")):
            res = await client.evaluate_policy(
                action="create_route",
                parameters={"route_name": "test"},
                blast_radius={"risk_score": 0.2},
                context={"environment": "development"},
            )
            # Should fallback to passing with a warning
            assert res["is_valid"]
            assert not res["errors"]
            assert any("Failed to reach Policy Engine" in w for w in res.get("warnings", []))

    @pytest.mark.asyncio
    async def test_evaluate_policy_unreachable_prod_fail_closed(self):
        client = OPAClient("http://localhost:8181")

        with patch("httpx.AsyncClient.post", side_effect=httpx.ConnectError("Connection refused")):
            res = await client.evaluate_policy(
                action="create_route",
                parameters={"route_name": "test"},
                blast_radius={"risk_score": 0.2},
                context={"environment": "production"},
            )
            # Should fail closed in production environment
            assert not res["is_valid"]
            assert any("Failed to reach Policy Engine in production" in e for e in res["errors"])


class TestSafetyServiceWithOPA:
    """Tests for SafetyService OPA integration."""

    @pytest.mark.asyncio
    async def test_validate_config_opa_allowed(self, safety_service_with_opa):
        parsed = ParsedConfiguration(
            action=IntentAction.CREATE_ROUTE,
            target_service="user-service",
            parameters={
                "route_name": "user-route",
                "target_cluster": "user-service-cluster",
            },
            reasoning="Valid route",
        )
        mock_opa_response = {
            "result": {
                "allow": True,
                "errors_list": [],
            }
        }

        with patch("httpx.AsyncClient.post") as mock_post:
            mock_post.return_value = AsyncMock(
                status_code=200,
                json=lambda: mock_opa_response,
                raise_for_status=lambda: None,
            )
            result = await safety_service_with_opa.validate_config(parsed, None)
            assert result.is_valid
            assert not result.errors

    @pytest.mark.asyncio
    async def test_validate_config_opa_denied(self, safety_service_with_opa):
        parsed = ParsedConfiguration(
            action=IntentAction.CREATE_ROUTE,
            target_service="user-service",
            parameters={
                "route_name": "user-route",
                "target_cluster": "user-service-cluster",
            },
            reasoning="Valid route",
        )
        mock_opa_response = {
            "result": {
                "allow": False,
                "errors_list": ["Blast radius risk score exceeds allowed threshold (0.60)."],
            }
        }

        with patch("httpx.AsyncClient.post") as mock_post:
            mock_post.return_value = AsyncMock(
                status_code=200,
                json=lambda: mock_opa_response,
                raise_for_status=lambda: None,
            )
            result = await safety_service_with_opa.validate_config(parsed, None)
            assert not result.is_valid
            assert "Blast radius risk score exceeds allowed threshold (0.60)." in result.errors
