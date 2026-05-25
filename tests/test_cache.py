"""
Unit tests for the response caching layer.
"""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

from broker.config import get_settings
from broker.schemas.intent import (
    BlastRadiusReport,
    IntentAction,
    ParsedConfiguration,
    ValidationResult,
)
from broker.services.intent_parser import IntentParserService


@pytest.fixture
def mock_llm():
    return AsyncMock()


@pytest.fixture
def mock_safety():
    mock = AsyncMock()
    mock.validate_config.return_value = ValidationResult(is_valid=True)
    mock.simulate_blast_radius.return_value = BlastRadiusReport(risk_score=0.1, is_safe=True)
    return mock


class TestResponseCache:
    """Tests for in-memory and Redis response cache integration."""

    @pytest.fixture(autouse=True)
    def restore_settings_and_clear_cache(self):
        """Reset the class-level cache and restore settings after each test."""
        IntentParserService._cache.clear()

        orig_enabled = get_settings().response_cache_enabled
        orig_redis = get_settings().redis_url

        yield

        get_settings().response_cache_enabled = orig_enabled
        get_settings().redis_url = orig_redis
        IntentParserService._cache.clear()

    @pytest.mark.asyncio
    async def test_in_memory_cache_hit(self, mock_llm, mock_safety):
        """Subsequent identical requests should hit the in-memory cache and avoid LLM gateway calls."""
        get_settings().response_cache_enabled = True
        get_settings().redis_url = None

        config = ParsedConfiguration(
            action=IntentAction.CREATE_ROUTE,
            target_service="test-service",
            parameters={"route_name": "test"},
            reasoning="Test reasoning",
        )
        mock_llm.parse_intent.return_value = config
        mock_llm.get_confidence_score.return_value = 0.9

        service = IntentParserService(mock_llm, mock_safety)

        # First call (cache miss)
        resp1 = await service.parse_and_validate("Create route test")
        assert resp1.parsed_configuration == config
        assert mock_llm.parse_intent.call_count == 1

        # Second call (cache hit)
        resp2 = await service.parse_and_validate("Create route test")
        assert resp2.parsed_configuration == config
        # LLM parse_intent should NOT have been called again
        assert mock_llm.parse_intent.call_count == 1

        # Verify that request_id is fresh for each cache hit
        assert resp1.request_id != resp2.request_id

    @pytest.mark.asyncio
    async def test_cache_disabled(self, mock_llm, mock_safety):
        """If response caching is disabled, LLM gateway should be queried every time."""
        get_settings().response_cache_enabled = False

        config = ParsedConfiguration(
            action=IntentAction.CREATE_ROUTE,
            target_service="test-service",
            parameters={"route_name": "test"},
            reasoning="Test reasoning",
        )
        mock_llm.parse_intent.return_value = config
        mock_llm.get_confidence_score.return_value = 0.9

        service = IntentParserService(mock_llm, mock_safety)

        # First call
        await service.parse_and_validate("Create route test")
        # Second call
        await service.parse_and_validate("Create route test")

        assert mock_llm.parse_intent.call_count == 2

    @pytest.mark.asyncio
    async def test_redis_cache_hit_and_store(self, mock_llm, mock_safety):
        """If redis_url is set, the service should query and store in Redis."""
        get_settings().response_cache_enabled = True
        get_settings().redis_url = "redis://localhost:6379"

        config = ParsedConfiguration(
            action=IntentAction.CREATE_ROUTE,
            target_service="test-service",
            parameters={"route_name": "test"},
            reasoning="Test reasoning",
        )
        mock_llm.parse_intent.return_value = config
        mock_llm.get_confidence_score.return_value = 0.9

        service = IntentParserService(mock_llm, mock_safety)

        mock_redis_client = AsyncMock()
        mock_redis_client.get.return_value = None  # Miss initially

        # Mock sys.modules for redis to avoid ModuleNotFoundError when running tests
        mock_redis = MagicMock()
        mock_redis.asyncio = MagicMock()
        mock_redis.asyncio.from_url.return_value = mock_redis_client
        sys.modules["redis"] = mock_redis
        sys.modules["redis.asyncio"] = mock_redis.asyncio

        try:
            # 1. Miss scenario: calls LLM and writes to Redis
            resp1 = await service.parse_and_validate("Create route test")
            assert mock_llm.parse_intent.call_count == 1
            assert mock_redis_client.get.call_count == 1
            assert mock_redis_client.setex.call_count == 1

            # Mock a Redis hit for the second call
            mock_redis_client.get.return_value = resp1.model_dump_json()
            mock_redis_client.get.call_count = 0  # reset count

            # Clear in-memory cache to guarantee Redis is queried
            IntentParserService._cache.clear()

            # 2. Hit scenario: queries Redis, bypasses LLM
            resp2 = await service.parse_and_validate("Create route test")
            assert mock_llm.parse_intent.call_count == 1  # Unchanged
            assert mock_redis_client.get.call_count == 1
            assert resp2.parsed_configuration == config
        finally:
            sys.modules.pop("redis", None)
            sys.modules.pop("redis.asyncio", None)
