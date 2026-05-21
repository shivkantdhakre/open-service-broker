"""
Shared test fixtures and configuration.

Provides mock AWS services (via moto), test clients, and common fixtures
used across all test modules.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator

import boto3
import pytest
from moto import mock_aws  # type: ignore[import-untyped]

# Set test environment before importing app
os.environ.update({
    "AWS_ENDPOINT_URL": "",
    "AWS_REGION": "us-east-1",
    "AWS_ACCESS_KEY_ID": "testing",
    "AWS_SECRET_ACCESS_KEY": "testing",
    "AWS_DEFAULT_REGION": "us-east-1",
    "LLM_PROVIDER": "stub",
    "LLM_API_KEY": "test-key",
    "DYNAMODB_TABLE_NAME": "test-broker-resources",
    "DYNAMODB_METRICS_TABLE_NAME": "test-broker-metrics",
    "SQS_QUEUE_URL": "http://localhost:4566/000000000000/test-broker-tasks",
    "SQS_DLQ_URL": "http://localhost:4566/000000000000/test-broker-tasks-dlq",
    "SOVEREIGN_API_URL": "http://localhost:9999",
})


@pytest.fixture(scope="session")
def event_loop() -> Iterator[asyncio.AbstractEventLoop]:
    """Create a session-scoped event loop for async tests."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def mock_dynamodb():
    """Create a mocked DynamoDB table using moto."""
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")

        # Create resources table
        client.create_table(
            TableName="test-broker-resources",
            AttributeDefinitions=[
                {"AttributeName": "resource_id", "AttributeType": "S"},
                {"AttributeName": "resource_type", "AttributeType": "S"},
            ],
            KeySchema=[
                {"AttributeName": "resource_id", "KeyType": "HASH"},
                {"AttributeName": "resource_type", "KeyType": "RANGE"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )

        # Create metrics table
        client.create_table(
            TableName="test-broker-metrics",
            AttributeDefinitions=[
                {"AttributeName": "service_name", "AttributeType": "S"},
                {"AttributeName": "timestamp", "AttributeType": "S"},
            ],
            KeySchema=[
                {"AttributeName": "service_name", "KeyType": "HASH"},
                {"AttributeName": "timestamp", "KeyType": "RANGE"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )

        yield client


@pytest.fixture
def mock_sqs():
    """Create a mocked SQS queue using moto."""
    with mock_aws():
        client = boto3.client("sqs", region_name="us-east-1")

        # Create task queue
        client.create_queue(QueueName="test-broker-tasks")

        # Create DLQ
        client.create_queue(QueueName="test-broker-tasks-dlq")

        yield client


@pytest.fixture
def settings():
    """Provide test settings."""
    from broker.config import Settings

    return Settings()


@pytest.fixture
def stub_llm_gateway():
    """Provide a stub LLM gateway for testing."""
    from broker.services.llm_gateway import StubLLMGateway

    return StubLLMGateway()


@pytest.fixture
def mock_event_bus():
    """Provide a fresh event bus for testing."""
    from broker.services.event_bus import EventBus

    return EventBus()
