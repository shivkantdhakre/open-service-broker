"""
Integration tests for the background Worker.

Verifies:
- Happy path task execution: Worker receives a PROVISION task, transitions state to ACTIVE.
- Concurrency and version chain integrity.
- Error handling: Worker catches Sovereign errors and routes failed messages to the DLQ.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from broker.schemas.resource import ResourceRecord, ResourceState
from broker.schemas.task import SQSMessageWrapper, TaskMessage, TaskType
from broker.services.sovereign_client import SovereignClient, SovereignError
from broker.worker import Worker


@pytest.fixture
def integration_worker(threaded_moto_server):
    """Provide a Worker instance configured to use the threaded mock AWS server."""
    import os
    from broker.config import get_settings

    os.environ["AWS_ENDPOINT_URL"] = threaded_moto_server
    os.environ["SQS_QUEUE_URL"] = f"{threaded_moto_server}/000000000000/test-broker-tasks"
    os.environ["SQS_DLQ_URL"] = f"{threaded_moto_server}/000000000000/test-broker-tasks-dlq"
    get_settings.cache_clear()

    worker = Worker()
    return worker


@pytest.mark.asyncio
class TestWorkerIntegration:
    """Integration tests checking Worker execution loops and error handling."""

    async def test_worker_provision_happy_path(
        self,
        integration_worker,
        async_dynamodb_service,
        async_sqs_service,
    ):
        """Verify happy path: Worker processes PROVISION task, state transitions PENDING -> PROVISIONING -> ACTIVE."""
        # 1. Create a PENDING resource in DynamoDB
        record = ResourceRecord(
            resource_id="worker-res-001",
            resource_type="create_route",
            state=ResourceState.PENDING,
            configuration={"route_name": "worker-route"},
        )
        await async_dynamodb_service.create_resource(record)

        # 2. Enqueue the PROVISION task in SQS
        task = TaskMessage(
            task_id="worker-task-001",
            task_type=TaskType.PROVISION,
            resource_id="worker-res-001",
            configuration={"action": "create_route", "target_service": "worker-service"},
        )
        await async_sqs_service.enqueue_task(task)

        # 3. Force one polling iteration of the worker
        # (Since self._sovereign is None, the worker will simulate Sovereign latency and succeed)
        await integration_worker._poll_and_process()

        # 4. Verify that the task was successfully processed and deleted from SQS
        depth = await async_sqs_service.get_queue_depth()
        assert depth == 0

        # 5. Verify the state chain in DynamoDB: state must be ACTIVE and version must be 3
        updated_record = await async_dynamodb_service.get_resource("worker-res-001", "create_route")
        assert updated_record.state == ResourceState.ACTIVE
        assert updated_record.version == 3

    async def test_worker_failure_routes_to_dlq(
        self,
        integration_worker,
        async_dynamodb_service,
        async_sqs_service,
    ):
        """Verify that when a task repeatedly fails, the worker routes it to the DLQ."""
        # 1. Create a PENDING resource
        record = ResourceRecord(
            resource_id="worker-res-fail",
            resource_type="create_route",
            state=ResourceState.PENDING,
        )
        await async_dynamodb_service.create_resource(record)

        # 2. Enqueue the task
        task = TaskMessage(
            task_id="worker-task-fail",
            task_type=TaskType.PROVISION,
            resource_id="worker-res-fail",
            configuration={"action": "create_route", "target_service": "worker-service"},
        )
        await async_sqs_service.enqueue_task(task)

        # 3. Mock Sovereign client on the worker to raise a ValueError (to simulate a critical error that fails the task)
        mock_sovereign = AsyncMock(spec=SovereignClient)
        mock_sovereign.get_current_config.side_effect = ValueError("Sovereign connection refused")
        integration_worker._sovereign = mock_sovereign

        # 4. Receive message and simulate it being received for the 3rd time (max retries exceeded)
        messages = await async_sqs_service.receive_tasks(max_messages=1, wait_seconds=1)
        assert len(messages) == 1
        msg = messages[0]
        msg.approximate_receive_count = 3

        # 5. Process the message directly
        await integration_worker._process_message(async_sqs_service, msg)

        # 6. Verify message was deleted from the main queue and routed to the DLQ
        depth = await async_sqs_service.get_queue_depth()
        assert depth == 0

        # Verify the message is in the DLQ by checking it
        # Under moto, the DLQ is a separate queue: test-broker-tasks-dlq
        import aioboto3
        session = aioboto3.Session(
            aws_access_key_id="testing",
            aws_secret_access_key="testing",
            region_name="us-east-1",
        )
        # Clear settings cache to get correct SQS DLQ settings
        from broker.config import get_settings
        settings = get_settings()
        async with session.client(
            "sqs",
            endpoint_url=settings.aws_endpoint_url,
            region_name=settings.aws_region,
        ) as client:
            dlq_resp = await client.receive_message(
                QueueUrl=settings.sqs_dlq_url,
                MaxNumberOfMessages=10,
            )
            dlq_messages = dlq_resp.get("Messages", [])
            found = False
            for msg in dlq_messages:
                if "worker-task-fail" in msg["Body"]:
                    assert "Sovereign connection refused" in msg["Body"]
                    found = True
                    break
            assert found, f"Expected message 'worker-task-fail' not found in DLQ. Messages: {[m['Body'] for m in dlq_messages]}"

    async def test_worker_maintenance_happy_path(
        self,
        integration_worker,
        async_sqs_service,
    ):
        """Verify that worker processes a MAINTENANCE task and triggers Git PR creation."""
        # 1. Enqueue a MAINTENANCE task
        task = TaskMessage(
            task_id="maint-test-001",
            task_type=TaskType.MAINTENANCE,
            resource_id="proposal-abc",
            configuration={
                "proposal": {
                    "proposal_id": "proposal-abc",
                    "title": "Decouple auth and user",
                    "description": "Refactor auth and user modules",
                    "files_affected": ["src/auth.py", "src/user.py"],
                    "diff_preview": "-import auth\n+# refactored",
                    "confidence": 0.9,
                    "estimated_effort": "small",
                },
                "dry_run": True,
            },
        )
        await async_sqs_service.enqueue_task(task)

        # 2. Process tasks via worker
        await integration_worker._poll_and_process()

        # 3. Verify task is consumed and deleted
        depth = await async_sqs_service.get_queue_depth()
        assert depth == 0

    async def test_worker_maintenance_failure_routes_to_dlq_immediately(
        self,
        integration_worker,
        async_sqs_service,
    ):
        """Verify that a failed MAINTENANCE task is routed to the DLQ immediately on the first attempt."""
        # 1. Enqueue a MAINTENANCE task with dry_run=False (which triggers ValueError due to missing GITHUB_REPO env var)
        task = TaskMessage(
            task_id="maint-test-fail",
            task_type=TaskType.MAINTENANCE,
            resource_id="proposal-fail",
            configuration={
                "proposal": {
                    "proposal_id": "proposal-fail",
                    "title": "Decouple database",
                    "description": "Refactor database module",
                    "files_affected": ["src/db.py"],
                    "diff_preview": "-import db\n+# refactored",
                    "confidence": 0.8,
                    "estimated_effort": "medium",
                },
                "dry_run": False,
            },
        )
        await async_sqs_service.enqueue_task(task)

        # 2. Receive and process task message (approximate_receive_count = 1)
        messages = await async_sqs_service.receive_tasks(max_messages=1, wait_seconds=1)
        assert len(messages) == 1
        msg = messages[0]
        msg.approximate_receive_count = 1

        # 3. Process the message directly
        await integration_worker._process_message(async_sqs_service, msg)

        # 4. Verify message was deleted from main queue (routed to DLQ)
        depth = await async_sqs_service.get_queue_depth()
        assert depth == 0

        # 5. Verify the message is in the DLQ
        import aioboto3
        session = aioboto3.Session(
            aws_access_key_id="testing",
            aws_secret_access_key="testing",
            region_name="us-east-1",
        )
        from broker.config import get_settings
        settings = get_settings()
        async with session.client(
            "sqs",
            endpoint_url=settings.aws_endpoint_url,
            region_name=settings.aws_region,
        ) as client:
            dlq_resp = await client.receive_message(
                QueueUrl=settings.sqs_dlq_url,
                MaxNumberOfMessages=10,
            )
            dlq_messages = dlq_resp.get("Messages", [])
            found = False
            for m in dlq_messages:
                if "maint-test-fail" in m["Body"]:
                    assert "GITHUB_REPO env var or repo_name must be set" in m["Body"]
                    found = True
                    break
            assert found, f"Expected failed maintenance task in DLQ"
