"""
Integration and simulation tests for red-team threat modeling.

Verifies that safety boundaries and OPA/Rego policies successfully block malicious
or accidental misconfigurations (wildcard routing, production deletions, sensitive paths).
"""

from __future__ import annotations

import httpx
import pytest
from unittest.mock import AsyncMock, patch

from broker.schemas.intent import IntentAction, ParsedConfiguration
from broker.services.safety import SafetyService


@pytest.fixture
def red_team_safety(settings):
    """Safety service with OPA enabled."""
    settings.opa_enabled = True
    settings.opa_url = "http://localhost:8181"
    mock_db = AsyncMock()
    return SafetyService(mock_db, settings)


class TestRedTeamThreatModeling:
    """Simulation tests attempting to violate security boundaries."""

    @pytest.mark.asyncio
    async def test_block_wildcard_route(self, red_team_safety):
        """OPA should reject wildcard route targeting '/' without constraints."""
        parsed = ParsedConfiguration(
            action=IntentAction.CREATE_ROUTE,
            target_service="ingress-service",
            parameters={"route_name": "malicious-wildcard", "prefix": "/", "target_cluster": "malicious-cluster"},
            reasoning="Attempting to hijack all traffic",
        )

        mock_opa_response = {
            "result": {
                "allow": False,
                "errors_list": [
                    "Wildcard route matching '/' without header or query parameter constraints is unsafe."
                ],
            }
        }

        with patch("httpx.AsyncClient.post") as mock_post:
            mock_post.return_value = AsyncMock(
                status_code=200,
                json=lambda: mock_opa_response,
                raise_for_status=lambda: None,
            )
            result = await red_team_safety.validate_config(parsed, None)
            assert not result.is_valid
            assert any("unsafe" in err.lower() for err in result.errors)

    @pytest.mark.asyncio
    async def test_block_production_delete(self, red_team_safety):
        """OPA should reject route deletions in production environment unless forced."""
        parsed = ParsedConfiguration(
            action=IntentAction.DELETE_ROUTE,
            target_service="critical-db-service",
            parameters={"route_name": "db-route"},
            reasoning="Attempting to delete route in production",
        )

        mock_opa_response = {
            "result": {
                "allow": False,
                "errors_list": [
                    "Route deletion in production is prohibited unless forced."
                ],
            }
        }

        with patch("httpx.AsyncClient.post") as mock_post:
            mock_post.return_value = AsyncMock(
                status_code=200,
                json=lambda: mock_opa_response,
                raise_for_status=lambda: None,
            )
            result = await red_team_safety.validate_config(
                parsed,
                None,
                context={"environment": "production"},
                force=False,
            )
            assert not result.is_valid
            assert any("prohibited" in err.lower() for err in result.errors)

    @pytest.mark.asyncio
    async def test_allow_production_delete_if_forced(self, red_team_safety):
        """OPA should allow production route deletions if force=True."""
        parsed = ParsedConfiguration(
            action=IntentAction.DELETE_ROUTE,
            target_service="critical-db-service",
            parameters={"route_name": "db-route"},
            reasoning="Forced route deletion in production by admin",
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
            result = await red_team_safety.validate_config(
                parsed,
                None,
                context={"environment": "production"},
                force=True,
            )
            assert result.is_valid

    @pytest.mark.asyncio
    async def test_block_sensitive_path_ingress(self, red_team_safety):
        """OPA should prevent public routing to sensitive path '/admin'."""
        parsed = ParsedConfiguration(
            action=IntentAction.CREATE_ROUTE,
            target_service="admin-portal",
            parameters={"route_name": "admin-route", "prefix": "/admin", "target_cluster": "admin-cluster"},
            reasoning="Exposing admin portal publicly",
        )

        mock_opa_response = {
            "result": {
                "allow": False,
                "errors_list": [
                    "Public ingress to sensitive path '/admin' is prohibited."
                ],
            }
        }

        with patch("httpx.AsyncClient.post") as mock_post:
            mock_post.return_value = AsyncMock(
                status_code=200,
                json=lambda: mock_opa_response,
                raise_for_status=lambda: None,
            )
            result = await red_team_safety.validate_config(parsed, None)
            assert not result.is_valid
            assert any("sensitive path '/admin'" in err for err in result.errors)
