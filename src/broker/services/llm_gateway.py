"""
LLM Gateway — abstract interface and provider implementations for AI-driven
natural language → structured configuration translation.

The gateway enforces schema-constrained generation to guarantee that the LLM
produces outputs conforming to our Pydantic models. All outputs are treated
as untrusted input and validated downstream by the safety service.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from typing import Any, TypeVar

import structlog
from pydantic import BaseModel, ValidationError

from broker.config import Settings
from broker.schemas.intent import IntentAction, ParsedConfiguration

logger = structlog.get_logger()

T = TypeVar("T", bound=BaseModel)

# ---------------------------------------------------------------------------
# System prompt for intent parsing
# ---------------------------------------------------------------------------
INTENT_SYSTEM_PROMPT = """\
You are an infrastructure configuration translator. Your job is to convert
natural language developer requests into structured JSON configurations for
an Envoy proxy control plane (Sovereign).

RULES:
1. You MUST output valid JSON matching the provided schema exactly.
2. You MUST select the most appropriate action from the allowed actions.
3. You MUST identify the target service from the developer's intent.
4. Parameters MUST be specific and actionable — never use placeholders.
5. If the intent is ambiguous, choose the safest interpretation and explain
   your reasoning.
6. NEVER fabricate service names, endpoints, or configuration that was not
   mentioned or implied by the developer.

ALLOWED ACTIONS:
- create_route: Create a new routing rule
- update_route: Modify an existing route (e.g., traffic splitting, canary)
- delete_route: Remove a routing rule
- configure_load_balancing: Set or change load balancing algorithm
- update_rate_limit: Configure request rate limiting
- create_cluster: Register a new upstream cluster
- update_cluster: Modify cluster endpoints or settings
- scale_service: Adjust service instance count
- configure_circuit_breaker: Set circuit breaker thresholds
- configure_retry_policy: Configure retry behavior
- configure_timeout: Set request/connection timeouts
"""

INTENT_FEW_SHOT_EXAMPLES = [
    {
        "input": "Route 30% of traffic from api-gateway to the canary deployment of user-service",
        "output": {
            "action": "update_route",
            "target_service": "user-service",
            "parameters": {
                "weighted_clusters": [
                    {"cluster_name": "user-service-stable", "weight": 70},
                    {"cluster_name": "user-service-canary", "weight": 30},
                ],
                "route_name": "api-gateway-to-user-service",
            },
            "reasoning": "Developer wants canary traffic splitting: 70% stable, 30% canary for user-service behind api-gateway.",
        },
    },
    {
        "input": "Set a rate limit of 1000 requests per minute on the /api/v1/payments endpoint",
        "output": {
            "action": "update_rate_limit",
            "target_service": "payments",
            "parameters": {
                "requests_per_unit": 1000,
                "unit": "minute",
                "target_route": "/api/v1/payments",
            },
            "reasoning": "Developer wants to enforce 1000 req/min rate limiting on the payments API endpoint.",
        },
    },
    {
        "input": "Enable round-robin load balancing for the order-service cluster",
        "output": {
            "action": "configure_load_balancing",
            "target_service": "order-service",
            "parameters": {
                "lb_policy": "ROUND_ROBIN",
                "cluster_name": "order-service",
            },
            "reasoning": "Developer wants to switch order-service cluster to round-robin load balancing.",
        },
    },
]


# ---------------------------------------------------------------------------
# Abstract Gateway Interface
# ---------------------------------------------------------------------------
class LLMGateway(ABC):
    """Abstract interface for LLM-backed intent parsing."""

    @abstractmethod
    async def parse_intent(self, text: str, context: dict[str, Any] | None = None) -> ParsedConfiguration:
        """Translate natural language into a structured ParsedConfiguration.

        Args:
            text: The developer's natural language request.
            context: Optional context hints (environment, namespace, etc.).

        Returns:
            A validated ParsedConfiguration instance.

        Raises:
            LLMParsingError: If the LLM fails to produce valid output after retries.
        """
        ...

    @abstractmethod
    async def get_confidence_score(self) -> float:
        """Return the confidence score of the last parse operation."""
        ...


class LLMParsingError(Exception):
    """Raised when the LLM fails to produce a valid structured output."""

    def __init__(self, message: str, raw_output: str | None = None) -> None:
        super().__init__(message)
        self.raw_output = raw_output


# ---------------------------------------------------------------------------
# OpenAI Implementation
# ---------------------------------------------------------------------------
class OpenAIGateway(LLMGateway):
    """LLM gateway implementation using OpenAI's API with structured outputs."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._last_confidence: float = 0.0
        self._client: Any = None

    def _get_client(self) -> Any:
        """Lazy-initialize the OpenAI async client."""
        if self._client is None:
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI(api_key=self._settings.llm_api_key)
        return self._client

    async def parse_intent(
        self,
        text: str,
        context: dict[str, Any] | None = None,
    ) -> ParsedConfiguration:
        """Parse natural language into structured config using OpenAI."""
        client = self._get_client()

        # Build the user message with context if provided
        user_message = f"Developer request: {text}"
        if context:
            user_message += f"\n\nContext: {json.dumps(context)}"

        # Build few-shot examples as conversation turns
        messages: list[dict[str, str]] = [
            {"role": "system", "content": INTENT_SYSTEM_PROMPT},
        ]
        for example in INTENT_FEW_SHOT_EXAMPLES:
            messages.append({"role": "user", "content": f"Developer request: {example['input']}"})
            messages.append({"role": "assistant", "content": json.dumps(example["output"])})
        messages.append({"role": "user", "content": user_message})

        # Retry loop for structured output
        max_retries = 3
        last_error: Exception | None = None

        for attempt in range(max_retries):
            try:
                response = await client.chat.completions.create(
                    model=self._settings.llm_model,
                    messages=messages,
                    temperature=self._settings.llm_temperature,
                    max_tokens=self._settings.llm_max_tokens,
                    response_format={"type": "json_object"},
                )

                raw_content = response.choices[0].message.content
                if not raw_content:
                    raise LLMParsingError("LLM returned empty response")

                await logger.ainfo(
                    "LLM raw response",
                    attempt=attempt + 1,
                    content_length=len(raw_content),
                )

                # Parse and validate against our schema
                parsed_data = json.loads(raw_content)
                config = ParsedConfiguration.model_validate(parsed_data)

                # Estimate confidence based on response characteristics
                self._last_confidence = self._estimate_confidence(config, text)

                return config

            except (json.JSONDecodeError, ValidationError) as e:
                last_error = e
                await logger.awarning(
                    "LLM output validation failed, retrying",
                    attempt=attempt + 1,
                    error=str(e),
                )
                # Add error feedback to help the LLM self-correct
                messages.append({
                    "role": "user",
                    "content": (
                        f"Your previous response was invalid: {e}. "
                        "Please try again with a valid JSON matching the schema."
                    ),
                })
                continue
            except Exception as e:
                last_error = e
                await logger.aerror(
                    "LLM API call failed",
                    attempt=attempt + 1,
                    error=str(e),
                )
                if attempt == max_retries - 1:
                    break
                continue

        raise LLMParsingError(
            f"Failed to parse intent after {max_retries} attempts: {last_error}",
            raw_output=None,
        )

    async def get_confidence_score(self) -> float:
        """Return confidence score of the last parse."""
        return self._last_confidence

    def _estimate_confidence(self, config: ParsedConfiguration, original: str) -> float:
        """Heuristic confidence estimation based on output quality signals."""
        score = 0.7  # Base confidence for valid structured output

        # Boost if action is common and well-represented in few-shot examples
        well_known_actions = {
            IntentAction.CREATE_ROUTE,
            IntentAction.UPDATE_ROUTE,
            IntentAction.UPDATE_RATE_LIMIT,
            IntentAction.CONFIGURE_LOAD_BALANCING,
        }
        if config.action in well_known_actions:
            score += 0.1

        # Boost if reasoning is substantive
        if len(config.reasoning) > 30:
            score += 0.1

        # Boost if parameters are populated
        if config.parameters:
            score += 0.1

        return min(score, 1.0)


# ---------------------------------------------------------------------------
# Schema sanitization helpers for Gemini structured outputs
# ---------------------------------------------------------------------------
def _inline_refs(schema_dict: dict, defs: dict | None = None) -> dict:
    """Recursively inline all ``$ref`` pointers using the top-level ``$defs``."""
    import copy as _copy

    if not isinstance(schema_dict, dict):
        return schema_dict

    if defs is None:
        defs = schema_dict.get("$defs", {})

    if "$ref" in schema_dict:
        ref_path = schema_dict["$ref"]
        if ref_path.startswith("#/$defs/"):
            ref_name = ref_path.split("/")[-1]
            ref_schema = _inline_refs(_copy.deepcopy(defs[ref_name]), defs)
            schema_dict.pop("$ref")
            for k, v in ref_schema.items():
                if k not in schema_dict:
                    schema_dict[k] = v

    for key, value in list(schema_dict.items()):
        if isinstance(value, dict):
            schema_dict[key] = _inline_refs(value, defs)
        elif isinstance(value, list):
            schema_dict[key] = [
                _inline_refs(item, defs) if isinstance(item, dict) else item
                for item in value
            ]

    return schema_dict


def _resolve_anyof(schema_dict: dict) -> dict:
    """Convert ``anyOf: [{type: T}, {type: null}]`` → ``{type: T, nullable: true}``."""
    if not isinstance(schema_dict, dict):
        return schema_dict

    if "anyOf" in schema_dict:
        anyof_list = schema_dict.pop("anyOf")
        non_null = [s for s in anyof_list if s.get("type") != "null"]
        if non_null:
            schema_dict.update(_resolve_anyof(non_null[0]))
            schema_dict["nullable"] = True
        else:
            schema_dict["type"] = "null"

    for key, value in list(schema_dict.items()):
        if isinstance(value, dict):
            schema_dict[key] = _resolve_anyof(value)
        elif isinstance(value, list):
            schema_dict[key] = [
                _resolve_anyof(item) if isinstance(item, dict) else item
                for item in value
            ]

    return schema_dict


_UNSUPPORTED_SCHEMA_KEYS = frozenset({
    "default", "title",
    "maximum", "minimum", "exclusiveMaximum", "exclusiveMinimum",
    "maxLength", "minLength", "pattern",
    "additionalProperties",
})


def _clean_schema(schema_dict: dict) -> None:
    """Recursively strip keys that Gemini's protobuf Schema rejects."""
    if not isinstance(schema_dict, dict):
        return

    for key in _UNSUPPORTED_SCHEMA_KEYS:
        schema_dict.pop(key, None)

    for value in schema_dict.values():
        if isinstance(value, dict):
            _clean_schema(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    _clean_schema(item)


def _pydantic_to_gemini_schema(model_cls: type[BaseModel]) -> dict:
    """Convert a Pydantic model class into a Gemini-compatible JSON schema dict."""
    schema = model_cls.model_json_schema()
    schema = _inline_refs(schema)
    schema.pop("$defs", None)
    schema = _resolve_anyof(schema)
    _clean_schema(schema)
    return schema


# ---------------------------------------------------------------------------
# Gemini Implementation
# ---------------------------------------------------------------------------
class GeminiGateway(LLMGateway):
    """LLM gateway implementation using Google Gemini with structured outputs."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._last_confidence: float = 0.0
        self._model: Any = None

    def _get_model(self) -> Any:
        """Lazy-initialize the Gemini GenerativeModel."""
        if self._model is None:
            import google.generativeai as genai

            genai.configure(api_key=self._settings.llm_api_key)

            response_schema = _pydantic_to_gemini_schema(ParsedConfiguration)

            generation_config = genai.GenerationConfig(
                response_mime_type="application/json",
                response_schema=response_schema,
                temperature=self._settings.llm_temperature,
                max_output_tokens=self._settings.llm_max_tokens,
            )

            self._model = genai.GenerativeModel(
                model_name=self._settings.llm_model,
                generation_config=generation_config,
                system_instruction=INTENT_SYSTEM_PROMPT,
            )
        return self._model

    async def parse_intent(
        self,
        text: str,
        context: dict[str, Any] | None = None,
    ) -> ParsedConfiguration:
        """Parse natural language into structured config using Gemini."""
        import asyncio

        model = self._get_model()

        # Build the user message with context if provided
        user_message = f"Developer request: {text}"
        if context:
            user_message += f"\n\nContext: {json.dumps(context)}"

        # Build few-shot conversation turns
        contents: list[dict[str, Any]] = []
        for example in INTENT_FEW_SHOT_EXAMPLES:
            contents.append({"role": "user", "parts": [f"Developer request: {example['input']}"]})
            contents.append({"role": "model", "parts": [json.dumps(example["output"])]})
        contents.append({"role": "user", "parts": [user_message]})

        # Retry loop for structured output
        max_retries = 3
        last_error: Exception | None = None

        for attempt in range(max_retries):
            try:
                # google-generativeai SDK is synchronous; run in a thread
                response = await asyncio.to_thread(model.generate_content, contents)

                raw_content = response.text
                if not raw_content:
                    raise LLMParsingError("Gemini returned empty response")

                await logger.ainfo(
                    "Gemini raw response",
                    attempt=attempt + 1,
                    content_length=len(raw_content),
                )

                # Parse and validate against our schema
                parsed_data = json.loads(raw_content)
                config = ParsedConfiguration.model_validate(parsed_data)

                # Estimate confidence
                self._last_confidence = self._estimate_confidence(config, text)

                return config

            except (json.JSONDecodeError, ValidationError) as e:
                last_error = e
                await logger.awarning(
                    "Gemini output validation failed, retrying",
                    attempt=attempt + 1,
                    error=str(e),
                )
                # Append self-correction feedback
                contents.append({
                    "role": "user",
                    "parts": [(
                        f"Your previous response was invalid: {e}. "
                        "Please try again with valid JSON matching the schema."
                    )],
                })
                continue
            except Exception as e:
                last_error = e
                await logger.aerror(
                    "Gemini API call failed",
                    attempt=attempt + 1,
                    error=str(e),
                )
                if attempt == max_retries - 1:
                    break
                continue

        raise LLMParsingError(
            f"Failed to parse intent after {max_retries} attempts: {last_error}",
            raw_output=None,
        )

    async def get_confidence_score(self) -> float:
        """Return confidence score of the last parse."""
        return self._last_confidence

    def _estimate_confidence(self, config: ParsedConfiguration, original: str) -> float:
        """Heuristic confidence estimation based on output quality signals."""
        score = 0.7  # Base confidence for valid structured output

        well_known_actions = {
            IntentAction.CREATE_ROUTE,
            IntentAction.UPDATE_ROUTE,
            IntentAction.UPDATE_RATE_LIMIT,
            IntentAction.CONFIGURE_LOAD_BALANCING,
        }
        if config.action in well_known_actions:
            score += 0.1

        if len(config.reasoning) > 30:
            score += 0.1

        if config.parameters:
            score += 0.1

        return min(score, 1.0)


# ---------------------------------------------------------------------------
# Stub Gateway (for testing without LLM API)
# ---------------------------------------------------------------------------
class StubLLMGateway(LLMGateway):
    """Stub gateway that returns predefined responses. Used for testing."""

    def __init__(self) -> None:
        self._last_confidence = 0.95

    async def parse_intent(
        self,
        text: str,
        context: dict[str, Any] | None = None,
    ) -> ParsedConfiguration:
        """Return a stub configuration for testing."""
        return ParsedConfiguration(
            action=IntentAction.CREATE_ROUTE,
            target_service="stub-service",
            parameters={"route_name": "stub-route", "prefix": "/users", "target_cluster": "stub-cluster"},
            reasoning=f"Stub response for: {text}",
        )

    async def get_confidence_score(self) -> float:
        return self._last_confidence


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------
def create_llm_gateway(settings: Settings) -> LLMGateway:
    """Create the appropriate LLM gateway based on configuration."""
    provider = settings.llm_provider.lower()

    if provider == "openai":
        return OpenAIGateway(settings)
    elif provider == "gemini":
        return GeminiGateway(settings)
    elif provider == "stub":
        logging.getLogger(__name__).warning("Using stub LLM gateway — not for production!")
        return StubLLMGateway()
    else:
        raise ValueError(
            f"Unsupported LLM provider: {provider}. "
            "Supported providers: openai, gemini, stub"
        )
