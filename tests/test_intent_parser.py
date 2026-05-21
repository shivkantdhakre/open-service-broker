"""
Tests for the AI intent parsing pipeline.

Tests the LLM gateway (stub), intent parser service, and the full
NL → validated configuration pipeline.
"""

from __future__ import annotations

import pytest

from broker.schemas.intent import IntentAction, ParsedConfiguration
from broker.services.llm_gateway import StubLLMGateway


@pytest.fixture
def stub_gateway():
    return StubLLMGateway()


class TestStubLLMGateway:
    """Tests for the stub LLM gateway."""

    @pytest.mark.asyncio
    async def test_parse_intent_returns_valid_config(self, stub_gateway):
        """Stub gateway should return a valid ParsedConfiguration."""
        result = await stub_gateway.parse_intent("Create a route for my service")

        assert isinstance(result, ParsedConfiguration)
        assert result.action == IntentAction.CREATE_ROUTE
        assert result.target_service == "stub-service"
        assert "route_name" in result.parameters
        assert len(result.reasoning) > 0

    @pytest.mark.asyncio
    async def test_parse_intent_includes_input_in_reasoning(self, stub_gateway):
        """Stub reasoning should reference the original input."""
        text = "Set rate limit to 500 for payments"
        result = await stub_gateway.parse_intent(text)

        assert text in result.reasoning

    @pytest.mark.asyncio
    async def test_confidence_score(self, stub_gateway):
        """Stub should return high confidence."""
        await stub_gateway.parse_intent("any input")
        score = await stub_gateway.get_confidence_score()

        assert 0.0 <= score <= 1.0
        assert score > 0.9  # Stub is always confident

    @pytest.mark.asyncio
    async def test_context_accepted(self, stub_gateway):
        """Gateway should accept optional context without errors."""
        context = {"environment": "staging", "namespace": "payments"}
        result = await stub_gateway.parse_intent("Create route", context=context)

        assert result is not None


class TestParsedConfiguration:
    """Tests for the ParsedConfiguration schema."""

    def test_valid_configuration(self):
        """Should accept valid configuration data."""
        config = ParsedConfiguration(
            action=IntentAction.CREATE_ROUTE,
            target_service="user-service",
            parameters={"route_name": "test-route"},
            reasoning="Test reasoning",
        )
        assert config.action == IntentAction.CREATE_ROUTE
        assert config.target_service == "user-service"

    def test_all_intent_actions(self):
        """All IntentAction values should be valid."""
        for action in IntentAction:
            config = ParsedConfiguration(
                action=action,
                target_service="test",
                parameters={},
                reasoning="test",
            )
            assert config.action == action

    def test_empty_parameters_allowed(self):
        """Parameters can be empty dict."""
        config = ParsedConfiguration(
            action=IntentAction.SCALE_SERVICE,
            target_service="test",
            parameters={},
            reasoning="test",
        )
        assert config.parameters == {}
