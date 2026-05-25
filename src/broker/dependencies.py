"""
FastAPI dependency injection providers.

Provides lazy-initialized, request-scoped access to core services:
DynamoDB, SQS, LLM Gateway, Event Bus, and Settings.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, Request

from broker.config import Settings, get_settings
from broker.services.dynamodb import DynamoDBService
from broker.services.event_bus import EventBus
from broker.services.llm_gateway import LLMGateway, create_llm_gateway
from broker.services.safety import SafetyService
from broker.services.sqs import SQSService

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
def get_app_settings() -> Settings:
    """Provide application settings."""
    return get_settings()


SettingsDep = Annotated[Settings, Depends(get_app_settings)]


# ---------------------------------------------------------------------------
# DynamoDB Service
# ---------------------------------------------------------------------------
async def get_dynamodb_service(request: Request) -> AsyncIterator[DynamoDBService]:
    """Provide a DynamoDB service instance using the shared session."""
    settings: Settings = request.app.state.settings
    session = request.app.state.aws_session
    async with session.resource(
        "dynamodb",
        endpoint_url=settings.aws_endpoint_url,
        region_name=settings.aws_region,
    ) as dynamodb:
        yield DynamoDBService(dynamodb, settings)


DynamoDBDep = Annotated[DynamoDBService, Depends(get_dynamodb_service)]


# ---------------------------------------------------------------------------
# SQS Service
# ---------------------------------------------------------------------------
async def get_sqs_service(request: Request) -> AsyncIterator[SQSService]:
    """Provide an SQS service instance using the shared session."""
    settings: Settings = request.app.state.settings
    session = request.app.state.aws_session
    async with session.client(
        "sqs",
        endpoint_url=settings.aws_endpoint_url,
        region_name=settings.aws_region,
    ) as sqs_client:
        yield SQSService(sqs_client, settings)


SQSDep = Annotated[SQSService, Depends(get_sqs_service)]


# ---------------------------------------------------------------------------
# LLM Gateway
# ---------------------------------------------------------------------------
def get_llm_gateway(settings: SettingsDep) -> LLMGateway:
    """Provide an LLM gateway instance based on configured provider."""
    return create_llm_gateway(settings)


LLMDep = Annotated[LLMGateway, Depends(get_llm_gateway)]


# ---------------------------------------------------------------------------
# Event Bus
# ---------------------------------------------------------------------------
def get_event_bus(request: Request) -> EventBus:
    """Provide the shared event bus instance."""
    return request.app.state.event_bus  # type: ignore[no-any-return]


EventBusDep = Annotated[EventBus, Depends(get_event_bus)]


# ---------------------------------------------------------------------------
# Safety Service
# ---------------------------------------------------------------------------
async def get_safety_service(
    dynamodb: DynamoDBDep,
    settings: SettingsDep,
) -> SafetyService:
    """Provide a safety service for blast radius analysis."""
    return SafetyService(dynamodb, settings)


SafetyDep = Annotated[SafetyService, Depends(get_safety_service)]
