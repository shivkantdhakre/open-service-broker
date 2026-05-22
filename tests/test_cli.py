"""
Tests for the ``osb`` CLI — commands, display, and error handling.

Uses Typer's ``CliRunner`` with mocked API responses so the tests run
without a live broker server.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from broker.cli.app import app
from broker.cli.config import CLIConfig, build_config

runner = CliRunner()


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------
MOCK_PARSE_RESPONSE: dict[str, Any] = {
    "request_id": "01J6FTEST000000000000000",
    "original_input": "Create a load balancer for payments",
    "parsed_configuration": {
        "action": "create_route",
        "target_service": "payments",
        "parameters": {
            "route_name": "payments-route",
            "target_cluster": "payments-cluster",
            "timeout_ms": 15000,
        },
        "reasoning": "User wants a route for the payments service.",
    },
    "validation": {
        "is_valid": True,
        "errors": [],
        "warnings": [],
    },
    "blast_radius": {
        "risk_score": 0.15,
        "affected_services": ["payments"],
        "affected_routes": [],
        "description": "Low risk change affecting a single service.",
        "is_safe": True,
    },
    "confidence_score": 0.92,
    "warnings": [],
    "created_at": "2026-05-22T10:00:00Z",
}

MOCK_APPLY_RESPONSE: dict[str, Any] = {
    "status": "accepted",
    "request_id": "01J6FTEST000000000000000",
    "resource_id": "01J6FTEST000000000000000",
    "message": "Configuration has been queued for provisioning.",
}

MOCK_RESOURCES: list[dict[str, Any]] = [
    {
        "resource_id": "res-001",
        "resource_type": "create_route",
        "state": "ACTIVE",
        "version": 3,
        "created_at": "2026-05-22T09:00:00Z",
        "updated_at": "2026-05-22T09:01:00Z",
    },
    {
        "resource_id": "res-002",
        "resource_type": "update_rate_limit",
        "state": "PENDING",
        "version": 1,
        "created_at": "2026-05-22T10:00:00Z",
        "updated_at": "2026-05-22T10:00:00Z",
    },
]

MOCK_HEALTH: dict[str, Any] = {"status": "ok"}


def _mock_client(**method_returns: Any) -> MagicMock:
    """Create a mock BrokerAPIClient with pre-configured return values."""
    client = AsyncMock()
    for method, return_value in method_returns.items():
        getattr(client, method).return_value = return_value
    return client


# ---------------------------------------------------------------------------
# Config tests
# ---------------------------------------------------------------------------
class TestCLIConfig:
    """Tests for CLIConfig."""

    def test_default_config(self):
        cfg = build_config()
        assert cfg.base_url == "http://localhost:8000"
        assert cfg.api_key == ""
        assert cfg.output_format == "rich"

    def test_custom_config(self):
        cfg = build_config(
            api_url="http://broker:9000/",
            api_key="sk-test-123",
            output_format="json",
            verbose=True,
        )
        assert cfg.base_url == "http://broker:9000"  # trailing slash stripped
        assert cfg.api_key == "sk-test-123"
        assert cfg.headers["X-API-Key"] == "sk-test-123"

    def test_headers_without_key(self):
        cfg = build_config(api_key="")
        assert "X-API-Key" not in cfg.headers

    def test_headers_with_key(self):
        cfg = build_config(api_key="sk-secret")
        assert cfg.headers["X-API-Key"] == "sk-secret"


# ---------------------------------------------------------------------------
# Intent commands
# ---------------------------------------------------------------------------
class TestIntentParse:
    """Tests for ``osb intent parse``."""

    @patch("broker.cli.app.BrokerAPIClient")
    def test_parse_rich_output(self, mock_cls: MagicMock):
        """Parse command should display a rich panel on success."""
        mock_instance = AsyncMock()
        mock_instance.parse_intent.return_value = MOCK_PARSE_RESPONSE
        mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
        mock_instance.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = mock_instance

        result = runner.invoke(app, ["intent", "parse", "Create a load balancer"])
        assert result.exit_code == 0
        assert "payments" in result.output or "Intent" in result.output

    @patch("broker.cli.app.BrokerAPIClient")
    def test_parse_json_output(self, mock_cls: MagicMock):
        """Parse command with --json should output raw JSON."""
        mock_instance = AsyncMock()
        mock_instance.parse_intent.return_value = MOCK_PARSE_RESPONSE
        mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
        mock_instance.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = mock_instance

        result = runner.invoke(app, ["--json", "intent", "parse", "Create a load balancer"])
        assert result.exit_code == 0
        # JSON output should contain the request_id
        assert "01J6FTEST" in result.output

    @patch("broker.cli.app.BrokerAPIClient")
    def test_parse_with_context(self, mock_cls: MagicMock):
        """Parse command should forward --env and --namespace context."""
        mock_instance = AsyncMock()
        mock_instance.parse_intent.return_value = MOCK_PARSE_RESPONSE
        mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
        mock_instance.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = mock_instance

        result = runner.invoke(
            app,
            ["intent", "parse", "Load balancer", "--env", "staging", "-n", "payments"],
        )
        assert result.exit_code == 0
        # Verify context was passed to the API
        call_args = mock_instance.parse_intent.call_args
        assert call_args.args[1] == {"environment": "staging", "namespace": "payments"}


# ---------------------------------------------------------------------------
# Resource commands
# ---------------------------------------------------------------------------
class TestResourceCommands:
    """Tests for ``osb resources`` subcommands."""

    @patch("broker.cli.app.BrokerAPIClient")
    def test_list_resources(self, mock_cls: MagicMock):
        mock_instance = AsyncMock()
        mock_instance.list_resources.return_value = MOCK_RESOURCES
        mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
        mock_instance.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = mock_instance

        result = runner.invoke(app, ["resources", "list"])
        assert result.exit_code == 0
        # Should display resource IDs
        assert "res-001" in result.output or "Resources" in result.output

    @patch("broker.cli.app.BrokerAPIClient")
    def test_show_resource(self, mock_cls: MagicMock):
        mock_instance = AsyncMock()
        mock_instance.get_resource.return_value = MOCK_RESOURCES[0]
        mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
        mock_instance.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = mock_instance

        result = runner.invoke(app, ["resources", "show", "res-001"])
        assert result.exit_code == 0

    @patch("broker.cli.app.BrokerAPIClient")
    def test_delete_resource_confirmed(self, mock_cls: MagicMock):
        mock_instance = AsyncMock()
        mock_instance.delete_resource.return_value = {"status": "deleted"}
        mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
        mock_instance.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = mock_instance

        result = runner.invoke(app, ["resources", "delete", "res-001", "--yes"])
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# Health command
# ---------------------------------------------------------------------------
class TestHealthCommand:
    """Tests for ``osb health``."""

    @patch("broker.cli.app.BrokerAPIClient")
    def test_health_check(self, mock_cls: MagicMock):
        mock_instance = AsyncMock()
        mock_instance.health.return_value = MOCK_HEALTH
        mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
        mock_instance.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = mock_instance

        result = runner.invoke(app, ["health"])
        assert result.exit_code == 0

    @patch("broker.cli.app.BrokerAPIClient")
    def test_health_json(self, mock_cls: MagicMock):
        mock_instance = AsyncMock()
        mock_instance.health.return_value = MOCK_HEALTH
        mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
        mock_instance.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = mock_instance

        result = runner.invoke(app, ["--json", "health"])
        assert result.exit_code == 0
        assert "ok" in result.output


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------
class TestErrorHandling:
    """Tests for CLI error handling."""

    @patch("broker.cli.app.BrokerAPIClient")
    def test_connection_error(self, mock_cls: MagicMock):
        """Connection errors should show a user-friendly message."""
        from broker.cli.api_client import ConnectionError as BrokerConnectionError

        mock_instance = AsyncMock()
        mock_instance.health.side_effect = BrokerConnectionError("Cannot connect")
        mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
        mock_instance.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = mock_instance

        result = runner.invoke(app, ["health"])
        assert result.exit_code == 1

    @patch("broker.cli.app.BrokerAPIClient")
    def test_api_error(self, mock_cls: MagicMock):
        """API errors should show the status code and detail."""
        from broker.cli.api_client import APIError

        mock_instance = AsyncMock()
        mock_instance.parse_intent.side_effect = APIError(422, "Invalid input")
        mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
        mock_instance.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = mock_instance

        result = runner.invoke(app, ["intent", "parse", "bad input"])
        assert result.exit_code == 1

    def test_no_args_shows_help(self):
        """Running osb with no args should show help."""
        result = runner.invoke(app, [])
        # Typer/Click returns exit code 2 when no_args_is_help triggers
        assert result.exit_code in (0, 2)
        assert "intent" in result.output
        assert "resources" in result.output
        assert "health" in result.output


# ---------------------------------------------------------------------------
# API Client unit tests
# ---------------------------------------------------------------------------
class TestSSEEvent:
    """Tests for SSE event parsing."""

    def test_sse_event_json_data(self):
        from broker.cli.api_client import SSEEvent

        event = SSEEvent(
            event="state_change",
            id="123",
            data='{"resource_id": "res-001", "state": "ACTIVE"}',
        )
        assert event.json_data["resource_id"] == "res-001"
        assert event.json_data["state"] == "ACTIVE"

    def test_sse_event_invalid_json(self):
        from broker.cli.api_client import SSEEvent

        event = SSEEvent(data="not-json")
        assert "raw" in event.json_data
        assert event.json_data["raw"] == "not-json"


# ---------------------------------------------------------------------------
# Display unit tests
# ---------------------------------------------------------------------------
class TestDisplayHelpers:
    """Tests for Rich display functions (smoke tests — verify no crashes)."""

    def test_display_intent_response(self):
        from broker.cli.display import display_intent_response
        # Should not raise
        display_intent_response(MOCK_PARSE_RESPONSE)

    def test_display_apply_response(self):
        from broker.cli.display import display_apply_response
        display_apply_response(MOCK_APPLY_RESPONSE)

    def test_display_resources_table(self):
        from broker.cli.display import display_resources_table
        display_resources_table(MOCK_RESOURCES)

    def test_display_resources_table_empty(self):
        from broker.cli.display import display_resources_table
        display_resources_table([])

    def test_display_health(self):
        from broker.cli.display import display_health
        display_health(MOCK_HEALTH)

    def test_display_error(self):
        from broker.cli.display import display_error
        display_error("Test error", "Some detail")

    def test_display_banner(self):
        from broker.cli.display import display_banner
        display_banner()

    def test_display_resource_detail(self):
        from broker.cli.display import display_resource_detail
        display_resource_detail({
            **MOCK_RESOURCES[0],
            "configuration": {"route_name": "test"},
            "created_by": "test-user",
        })

    def test_display_intent_history(self):
        from broker.cli.display import display_intent_history
        display_intent_history([
            {
                "request_id": "req-001",
                "action": "create_route",
                "target_service": "payments",
                "status": "ACTIVE",
                "created_at": "2026-05-22T10:00:00Z",
            }
        ])
