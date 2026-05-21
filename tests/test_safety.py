"""
Tests for the Safety Service — deterministic validation and blast radius simulation.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from broker.schemas.intent import IntentAction, ParsedConfiguration
from broker.schemas.sovereign import RouteConfig, RouteMatch, WeightedCluster
from broker.services.safety import SafetyService


@pytest.fixture
def safety_service(settings):
    mock_db = AsyncMock()
    return SafetyService(mock_db, settings)


class TestValidateConfig:
    """Tests for deterministic configuration validation."""

    @pytest.mark.asyncio
    async def test_valid_route_config(self, safety_service):
        """A well-formed route config should pass validation."""
        parsed = ParsedConfiguration(
            action=IntentAction.CREATE_ROUTE,
            target_service="user-service",
            parameters={
                "route_name": "user-route",
                "target_cluster": "user-service-cluster",
            },
            reasoning="Create route for user service",
        )
        route = RouteConfig(
            route_name="user-route",
            match=RouteMatch(prefix="/api/v1/users"),
            target_cluster="user-service-cluster",
        )

        result = await safety_service.validate_config(parsed, route)

        assert result.is_valid
        assert len(result.errors) == 0

    @pytest.mark.asyncio
    async def test_empty_target_service_rejected(self, safety_service):
        """Empty target service should fail validation."""
        parsed = ParsedConfiguration(
            action=IntentAction.CREATE_ROUTE,
            target_service="",
            parameters={"target_cluster": "test"},
            reasoning="test",
        )

        result = await safety_service.validate_config(parsed, None)

        assert not result.is_valid
        assert any("empty" in e.lower() for e in result.errors)

    @pytest.mark.asyncio
    async def test_invalid_service_name_rejected(self, safety_service):
        """Service names with special characters should fail."""
        parsed = ParsedConfiguration(
            action=IntentAction.CREATE_ROUTE,
            target_service="user-service; DROP TABLE users;--",
            parameters={"target_cluster": "test"},
            reasoning="test",
        )

        result = await safety_service.validate_config(parsed, None)

        assert not result.is_valid

    @pytest.mark.asyncio
    async def test_wildcard_route_flagged_as_dangerous(self, safety_service):
        """A wildcard route matching all traffic should be flagged."""
        parsed = ParsedConfiguration(
            action=IntentAction.CREATE_ROUTE,
            target_service="user-service",
            parameters={"target_cluster": "test"},
            reasoning="test",
        )
        route = RouteConfig(
            route_name="wildcard-route",
            match=RouteMatch(prefix="/"),
            target_cluster="user-service",
        )

        result = await safety_service.validate_config(parsed, route)

        # Should have a warning or error about wildcard pattern
        assert len(result.errors) > 0 or len(result.warnings) > 0

    @pytest.mark.asyncio
    async def test_unbalanced_weights_rejected(self, safety_service):
        """Weighted clusters not summing to 100 should be flagged."""
        parsed = ParsedConfiguration(
            action=IntentAction.UPDATE_ROUTE,
            target_service="user-service",
            parameters={
                "weighted_clusters": [
                    {"cluster_name": "a", "weight": 50},
                    {"cluster_name": "b", "weight": 30},
                ],
            },
            reasoning="test",
        )
        route = RouteConfig(
            route_name="test-route",
            weighted_clusters=[
                WeightedCluster(cluster_name="a", weight=50),
                WeightedCluster(cluster_name="b", weight=30),
            ],
        )

        result = await safety_service.validate_config(parsed, route)

        assert not result.is_valid
        assert any("weight" in e.lower() for e in result.errors)

    @pytest.mark.asyncio
    async def test_rate_limit_missing_requests_per_unit(self, safety_service):
        """Rate limit action without requests_per_unit should fail."""
        parsed = ParsedConfiguration(
            action=IntentAction.UPDATE_RATE_LIMIT,
            target_service="payments",
            parameters={"unit": "minute"},
            reasoning="test",
        )

        result = await safety_service.validate_config(parsed, None)

        assert not result.is_valid
        assert any("requests_per_unit" in e for e in result.errors)


class TestBlastRadiusSimulation:
    """Tests for blast radius analysis."""

    @pytest.mark.asyncio
    async def test_low_risk_simple_route(self, safety_service):
        """A simple route creation should have low risk."""
        parsed = ParsedConfiguration(
            action=IntentAction.CREATE_ROUTE,
            target_service="user-service",
            parameters={
                "route_name": "user-route",
                "target_cluster": "user-cluster",
                "prefix": "/api/v1/users",
            },
            reasoning="test",
        )

        report = await safety_service.simulate_blast_radius(parsed)

        assert report.risk_score < 0.5
        assert report.is_safe

    @pytest.mark.asyncio
    async def test_high_risk_route_deletion(self, safety_service):
        """Deleting a route should carry higher risk."""
        parsed = ParsedConfiguration(
            action=IntentAction.DELETE_ROUTE,
            target_service="critical-service",
            parameters={"route_name": "main-route"},
            reasoning="test",
        )

        report = await safety_service.simulate_blast_radius(parsed)

        assert report.risk_score >= 0.5

    @pytest.mark.asyncio
    async def test_root_path_increases_risk(self, safety_service):
        """Configurations targeting root path should increase risk score."""
        parsed = ParsedConfiguration(
            action=IntentAction.UPDATE_RATE_LIMIT,
            target_service="gateway",
            parameters={
                "target_route": "/",
                "requests_per_unit": 100,
            },
            reasoning="test",
        )

        report = await safety_service.simulate_blast_radius(parsed)

        assert report.risk_score > 0.0

    @pytest.mark.asyncio
    async def test_traffic_split_lists_all_clusters(self, safety_service):
        """Traffic split should list all affected clusters in the report."""
        parsed = ParsedConfiguration(
            action=IntentAction.UPDATE_ROUTE,
            target_service="api-gateway",
            parameters={
                "route_name": "canary-route",
                "weighted_clusters": [
                    {"cluster_name": "stable", "weight": 80},
                    {"cluster_name": "canary", "weight": 20},
                ],
            },
            reasoning="test",
        )

        report = await safety_service.simulate_blast_radius(parsed)

        assert "stable" in report.affected_services
        assert "canary" in report.affected_services
