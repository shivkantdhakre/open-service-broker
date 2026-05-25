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


class TestIntentParserService:
    """Tests for the IntentParserService pipeline and feedback loop."""

    @pytest.mark.asyncio
    async def test_parse_and_validate_success_first_attempt(self):
        from unittest.mock import AsyncMock
        from broker.services.intent_parser import IntentParserService
        from broker.schemas.intent import ValidationResult, BlastRadiusReport

        # Mock LLM and Safety
        mock_llm = AsyncMock()
        mock_safety = AsyncMock()

        config = ParsedConfiguration(
            action=IntentAction.CREATE_ROUTE,
            target_service="user-service",
            parameters={"route_name": "user-route"},
            reasoning="Parse first attempt",
        )
        mock_llm.parse_intent.return_value = config
        mock_llm.get_confidence_score.return_value = 0.95

        mock_safety.validate_config.return_value = ValidationResult(is_valid=True)
        mock_safety.simulate_blast_radius.return_value = BlastRadiusReport(
            risk_score=0.2, affected_services=["user-service"], is_safe=True
        )

        service = IntentParserService(mock_llm, mock_safety)
        response = await service.parse_and_validate("Create user route")

        assert response.validation.is_valid
        assert response.parsed_configuration == config
        mock_llm.parse_intent.assert_called_once()
        mock_safety.validate_config.assert_called_once()

    @pytest.mark.asyncio
    async def test_parse_and_validate_self_correction_loop(self):
        from unittest.mock import AsyncMock
        from broker.services.intent_parser import IntentParserService
        from broker.schemas.intent import ValidationResult, BlastRadiusReport

        mock_llm = AsyncMock()
        mock_safety = AsyncMock()

        # First attempt returns bad config; second attempt returns good config
        bad_config = ParsedConfiguration(
            action=IntentAction.CREATE_ROUTE,
            target_service="bad-service",
            parameters={},
            reasoning="First bad attempt",
        )
        good_config = ParsedConfiguration(
            action=IntentAction.CREATE_ROUTE,
            target_service="good-service",
            parameters={"route_name": "good-route", "target_cluster": "good-cluster"},
            reasoning="Second good attempt",
        )
        mock_llm.parse_intent.side_effect = [bad_config, good_config]
        mock_llm.get_confidence_score.return_value = 0.85

        # First validation fails, second succeeds
        fail_validation = ValidationResult(is_valid=False, errors=["Missing route_name parameter"])
        success_validation = ValidationResult(is_valid=True)
        mock_safety.validate_config.side_effect = [fail_validation, success_validation]

        mock_safety.simulate_blast_radius.return_value = BlastRadiusReport(
            risk_score=0.1, affected_services=["good-service"], is_safe=True
        )

        service = IntentParserService(mock_llm, mock_safety)
        response = await service.parse_and_validate("Create route")

        assert response.validation.is_valid
        assert response.parsed_configuration.target_service == "good-service"
        # parse_intent should have been called twice
        assert mock_llm.parse_intent.call_count == 2
        assert mock_safety.validate_config.call_count == 2

        # Check that context passed on second call includes validation_feedback
        second_call_context = mock_llm.parse_intent.call_args_list[1][0][1]
        assert "validation_feedback" in second_call_context
        assert second_call_context["validation_feedback"]["previous_errors"] == ["Missing route_name parameter"]

