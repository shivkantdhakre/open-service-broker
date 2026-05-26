"""
Tests for the Gemini LLM Gateway and its schema sanitization pipeline.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from broker.config import Settings
from broker.schemas.intent import IntentAction, ParsedConfiguration
from broker.services.llm_gateway import (
    GeminiGateway,
    _clean_schema,
    _inline_refs,
    _pydantic_to_gemini_schema,
    _resolve_anyof,
    create_llm_gateway,
)


def test_schema_inlining():
    """Test that $ref pointers are recursively inlined using $defs."""
    schema = {
        "$defs": {
            "SubModel": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
            }
        },
        "type": "object",
        "properties": {
            "sub": {"$ref": "#/$defs/SubModel"},
        },
    }

    inlined = _inline_refs(schema)
    assert "$ref" not in inlined["properties"]["sub"]
    assert inlined["properties"]["sub"]["type"] == "object"
    assert inlined["properties"]["sub"]["properties"]["name"]["type"] == "string"


def test_resolve_anyof():
    """Test that anyOf: [{type: T}, {type: null}] is simplified to type: T, nullable: true."""
    schema = {
        "properties": {
            "maybe_str": {
                "anyOf": [
                    {"type": "string"},
                    {"type": "null"}
                ]
            }
        }
    }

    resolved = _resolve_anyof(schema)
    prop = resolved["properties"]["maybe_str"]
    assert "anyOf" not in prop
    assert prop["type"] == "string"
    assert prop["nullable"] is True


def test_clean_schema():
    """Test that unsupported keys are stripped from the schema."""
    schema = {
        "type": "object",
        "title": "MySchema",
        "default": {},
        "additionalProperties": False,
        "properties": {
            "count": {
                "type": "integer",
                "minimum": 0,
                "maximum": 100,
                "default": 10,
            }
        }
    }

    _clean_schema(schema)
    assert "title" not in schema
    assert "default" not in schema
    assert "additionalProperties" not in schema

    count_prop = schema["properties"]["count"]
    assert "minimum" not in count_prop
    assert "maximum" not in count_prop
    assert "default" not in count_prop
    assert count_prop["type"] == "integer"


def test_pydantic_to_gemini_schema():
    """Test full pipeline conversion of ParsedConfiguration model."""
    schema = _pydantic_to_gemini_schema(ParsedConfiguration)

    # Assert top-level keys
    assert "$defs" not in schema
    assert "$ref" not in schema
    assert "title" not in schema
    assert "default" not in schema

    # Check that it cleaned nested structures like parameters
    assert "properties" in schema
    params_schema = schema["properties"]["parameters"]
    assert "$ref" not in params_schema
    assert "anyOf" not in params_schema


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_gemini_gateway_lazy_initialization():
    """Test that GeminiGateway lazily configures genai and creates the model."""
    settings = Settings(
        llm_provider="gemini",
        llm_api_key="mock-api-key-123",
        llm_model="gemini-3.5-flash",
        llm_temperature=0.2,
        llm_max_tokens=2048,
    )

    gateway = GeminiGateway(settings)
    assert gateway._client is None

    with patch("google.genai.Client") as mock_client_cls:
        client = gateway._get_client()

        mock_client_cls.assert_called_once_with(api_key="mock-api-key-123")
        assert gateway._client is not None
        assert client == gateway._client


@pytest.mark.asyncio
async def test_gemini_gateway_parse_intent_success():
    """Test happy path of parse_intent in GeminiGateway."""
    settings = Settings(
        llm_provider="gemini",
        llm_api_key="mock-api-key",
        llm_model="gemini-3.5-flash",
    )

    gateway = GeminiGateway(settings)

    mock_client = MagicMock()
    mock_response = MagicMock()
    # Mocking Gemini response containing JSON matching ParsedConfiguration
    mock_response.text = json.dumps({
        "action": "create_route",
        "target_service": "user-service",
        "parameters": {
            "route_name": "user-route",
            "prefix": "/users",
            "target_cluster": "user-cluster"
        },
        "reasoning": "Creating route for user-service as requested."
    })
    mock_generate_content = AsyncMock(return_value=mock_response)
    mock_client.aio.models.generate_content = mock_generate_content
    gateway._client = mock_client

    result = await gateway.parse_intent("Create a route for user-service to cluster user-cluster")

    assert isinstance(result, ParsedConfiguration)
    assert result.action == IntentAction.CREATE_ROUTE
    assert result.target_service == "user-service"
    assert result.parameters["route_name"] == "user-route"
    assert result.parameters["target_cluster"] == "user-cluster"

    score = await gateway.get_confidence_score()
    assert score > 0.7


@pytest.mark.asyncio
async def test_gemini_gateway_parse_intent_retry_logic():
    """Test that GeminiGateway retries if JSON parsing or validation fails."""
    settings = Settings(
        llm_provider="gemini",
        llm_api_key="mock-api-key",
        llm_model="gemini-3.5-flash",
    )

    gateway = GeminiGateway(settings)

    mock_client = MagicMock()
    # First response: invalid JSON
    # Second response: valid JSON but invalid schema (missing target_service)
    # Third response: fully valid JSON
    mock_resp1 = MagicMock()
    mock_resp1.text = "invalid json text"

    mock_resp2 = MagicMock()
    mock_resp2.text = json.dumps({
        "action": "create_route",
        # missing target_service
        "parameters": {},
        "reasoning": "Missing service."
    })

    mock_resp3 = MagicMock()
    mock_resp3.text = json.dumps({
        "action": "create_route",
        "target_service": "auth-service",
        "parameters": {"route_name": "auth-route"},
        "reasoning": "Success on third try."
    })

    mock_generate_content = AsyncMock(side_effect=[mock_resp1, mock_resp2, mock_resp3])
    mock_client.aio.models.generate_content = mock_generate_content
    gateway._client = mock_client

    result = await gateway.parse_intent("some request")
    assert result.target_service == "auth-service"
    assert mock_generate_content.call_count == 3


def test_factory_resolves_gemini():
    """Test that create_llm_gateway factory registers and returns GeminiGateway."""
    settings = Settings(
        llm_provider="gemini",
        llm_api_key="mock-api-key",
        llm_model="gemini-3.5-flash",
    )

    gateway = create_llm_gateway(settings)
    assert isinstance(gateway, GeminiGateway)
