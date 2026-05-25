"""
Shared test fixtures and configuration.

Provides mock AWS services (via moto), test clients, and common fixtures
used across all test modules.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from typing import TYPE_CHECKING

import boto3
import pytest
from moto import mock_aws  # type: ignore[import-untyped]
from moto.server import ThreadedMotoServer

if TYPE_CHECKING:
    from collections.abc import Iterator

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



@pytest.fixture(scope="session")
def threaded_moto_server():
    """Run a local mocked AWS server in a background thread."""
    server = ThreadedMotoServer(port=0)
    server.start()

    host = server._server.server_address[0]
    if host == "0.0.0.0":
        host = "127.0.0.1"
    port = server._server.server_address[1]
    endpoint_url = f"http://{host}:{port}"

    # Wait for the server to become ready
    import time
    import urllib.error
    import urllib.request

    for _ in range(50):
        try:
            # Hit the root of the moto server
            urllib.request.urlopen(endpoint_url)
            break
        except urllib.error.URLError:
            time.sleep(0.1)

    yield endpoint_url

    server.stop()


@pytest.fixture
async def async_dynamodb_service(settings, threaded_moto_server):
    """Provide a DynamoDBService instance connected to the mocked database."""
    import os

    import aioboto3

    from broker.config import get_settings
    from broker.services.dynamodb import DynamoDBService

    # Clear config cache and update endpoint env var
    os.environ["AWS_ENDPOINT_URL"] = threaded_moto_server
    get_settings.cache_clear()

    # Create the table structure in the mock database
    client = boto3.client("dynamodb", region_name="us-east-1", endpoint_url=threaded_moto_server)
    with contextlib.suppress(client.exceptions.ResourceInUseException):
        client.create_table(
            TableName=settings.dynamodb_table_name,
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

    session = aioboto3.Session(
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
        region_name="us-east-1",
    )
    async with session.resource(
        "dynamodb",
        endpoint_url=threaded_moto_server,
        region_name="us-east-1",
    ) as dynamodb:
        yield DynamoDBService(dynamodb, get_settings())


@pytest.fixture
async def async_sqs_service(settings, threaded_moto_server):
    """Provide an SQSService instance connected to the mocked SQS queue."""
    import json
    import os

    import aioboto3

    from broker.config import get_settings
    from broker.services.sqs import SQSService

    # Clear config cache and update env vars
    os.environ["AWS_ENDPOINT_URL"] = threaded_moto_server
    os.environ["SQS_QUEUE_URL"] = f"{threaded_moto_server}/000000000000/test-broker-tasks"
    os.environ["SQS_DLQ_URL"] = f"{threaded_moto_server}/000000000000/test-broker-tasks-dlq"
    get_settings.cache_clear()
    current_settings = get_settings()

    client = boto3.client("sqs", region_name="us-east-1", endpoint_url=threaded_moto_server)
    # Create queue and DLQ in the mock service
    try:
        client.create_queue(QueueName="test-broker-tasks-dlq")
        # Get DLQ ARN
        dlq_url = client.get_queue_url(QueueName="test-broker-tasks-dlq")["QueueUrl"]
        attrs = client.get_queue_attributes(QueueUrl=dlq_url, AttributeNames=["QueueArn"])
        dlq_arn = attrs["Attributes"]["QueueArn"]

        # Create main queue with redrive policy
        redrive_policy = json.dumps({
            "deadLetterTargetArn": dlq_arn,
            "maxReceiveCount": "3"
        })
        client.create_queue(
            QueueName="test-broker-tasks",
            Attributes={"RedrivePolicy": redrive_policy}
        )
    except Exception:
        pass

    session = aioboto3.Session(
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
        region_name="us-east-1",
    )
    async with session.client(
        "sqs",
        endpoint_url=threaded_moto_server,
        region_name="us-east-1",
    ) as sqs_client:
        yield SQSService(sqs_client, current_settings)
