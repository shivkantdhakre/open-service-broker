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
from broker.schemas.task import TaskMessage, TaskType
from broker.services.sovereign_client import SovereignClient
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
        mock_sovereign.get_route.side_effect = ValueError("Sovereign connection refused")
        mock_sovereign.apply_route.side_effect = ValueError("Sovereign connection refused")
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
            assert found, "Expected failed maintenance task in DLQ"

    async def test_worker_idempotency_skip(
        self,
        integration_worker,
        async_dynamodb_service,
        async_sqs_service,
    ):
        """Verify that when Sovereign matches the target config, worker skips applying and self-heals DB state."""
        # 1. Create a PENDING resource in DynamoDB
        record = ResourceRecord(
            resource_id="worker-res-idemp",
            resource_type="create_route",
            state=ResourceState.PENDING,
            configuration={"route_name": "idemp-route", "target_cluster": "test-cluster"},
        )
        await async_dynamodb_service.create_resource(record)

        # 2. Enqueue the task
        task = TaskMessage(
            task_id="worker-task-idemp",
            task_type=TaskType.PROVISION,
            resource_id="worker-res-idemp",
            configuration={
                "action": "create_route",
                "target_service": "test-service",
                "parameters": {"route_name": "idemp-route", "target_cluster": "test-cluster"},
            },
        )
        await async_sqs_service.enqueue_task(task)

        # 3. Mock Sovereign client
        mock_sovereign = AsyncMock(spec=SovereignClient)
        # Returns exact matching payload
        mock_sovereign.get_route.return_value = {
            "route_name": "idemp-route",
            "match": {"prefix": "/", "headers": {}, "query_parameters": {}},
            "target_cluster": "test-cluster",
            "weighted_clusters": None,
            "timeout_ms": 15000,
            "retry_on": None,
            "max_retries": 1,
            "metadata": {},
        }
        integration_worker._sovereign = mock_sovereign

        # 4. Process task
        await integration_worker._poll_and_process()

        # 5. Verify apply_route was NOT called (skipped due to match)
        mock_sovereign.apply_route.assert_not_called()

        # 6. Verify state was self-healed in DynamoDB to ACTIVE
        updated_record = await async_dynamodb_service.get_resource("worker-res-idemp", "create_route")
        assert updated_record.state == ResourceState.ACTIVE
        assert updated_record.version == 3  # PENDING (1) -> PROVISIONING (2) -> ACTIVE (3)

    async def test_worker_dlq_publish_event(
        self,
        integration_worker,
        async_dynamodb_service,
        async_sqs_service,
    ):
        """Verify that when a task is routed to the DLQ, the worker posts an anomaly event via HTTP."""
        from unittest.mock import MagicMock, patch

        # 1. Create a PENDING resource
        record = ResourceRecord(
            resource_id="worker-res-dlq-event",
            resource_type="create_route",
            state=ResourceState.PENDING,
        )
        await async_dynamodb_service.create_resource(record)

        # 2. Enqueue the task
        task = TaskMessage(
            task_id="worker-task-dlq-event",
            task_type=TaskType.PROVISION,
            resource_id="worker-res-dlq-event",
            configuration={"action": "create_route", "target_service": "worker-service"},
            correlation_id="corr-12345",
        )
        await async_sqs_service.enqueue_task(task)

        # 3. Mock Sovereign client to fail
        mock_sovereign = AsyncMock(spec=SovereignClient)
        mock_sovereign.get_current_config.side_effect = ValueError("Sovereign connection refused")
        mock_sovereign.get_route.side_effect = ValueError("Sovereign connection refused")
        mock_sovereign.apply_route.side_effect = ValueError("Sovereign connection refused")
        integration_worker._sovereign = mock_sovereign

        # 4. Process the message simulating 3rd retry
        messages = await async_sqs_service.receive_tasks(max_messages=1, wait_seconds=1)
        assert len(messages) == 1
        msg = messages[0]
        msg.approximate_receive_count = 3

        # 5. Patch httpx.AsyncClient.post
        mock_post = AsyncMock()
        mock_post.return_value = MagicMock(status_code=200)

        with patch("httpx.AsyncClient.post", mock_post):
            await integration_worker._process_message(async_sqs_service, msg)

        # 6. Verify httpx POST was made to the publish endpoint with correct payload
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        url = args[0]
        assert "/api/v1/events/publish" in url

        event_payload = kwargs["json"]
        assert event_payload["event_type"] == "anomaly"
        assert event_payload["resource_id"] == "worker-res-dlq-event"
        assert event_payload["state"] == "FAILED"
        assert event_payload["data"]["correlation_id"] == "corr-12345"
        assert "Sovereign connection refused" in event_payload["data"]["error"]

    async def test_dlq_poller_task(self, threaded_moto_server):
        """Verify that the DLQ depth poller publishes an anomaly event when messages are in the DLQ."""
        import contextlib

        import aioboto3
        import boto3

        from broker.config import Settings
        from broker.main import poll_dlq_depth
        from broker.services.event_bus import EventBus

        # 1. Initialize mock SQS queues using boto3
        sqs_client = boto3.client("sqs", region_name="us-east-1", endpoint_url=threaded_moto_server)
        # Create unique DLQ for this test
        q_name = "test-dlq-poller-dlq"
        sqs_client.create_queue(QueueName=q_name)
        dlq_url = sqs_client.get_queue_url(QueueName=q_name)["QueueUrl"]

        # Send a message to DLQ
        sqs_client.send_message(QueueUrl=dlq_url, MessageBody="failed task payload")

        # 2. Setup poller dependencies
        test_settings = Settings(
            aws_endpoint_url=threaded_moto_server,
            sqs_dlq_url=dlq_url,
        )

        session = aioboto3.Session(
            aws_access_key_id="testing",
            aws_secret_access_key="testing",
            region_name="us-east-1",
        )

        event_bus = EventBus()

        # Collect events from EventBus
        received_events = []
        async def mock_subscribe():
            async for ev in event_bus.subscribe("test-sub"):
                received_events.append(ev)
                break

        sub_task = asyncio.create_task(mock_subscribe())
        await asyncio.sleep(0.01)

        # Run poller task and cancel it after it completes one poll iteration
        poller = asyncio.create_task(poll_dlq_depth(event_bus, session, test_settings, interval=0.01))

        # Wait for the subscription task to complete (meaning event has been received!)
        await asyncio.wait_for(sub_task, timeout=5.0)

        poller.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await poller

        assert len(received_events) == 1
        assert received_events[0].event_type == "anomaly"
        assert received_events[0].resource_id == "dlq"
        assert received_events[0].data["dlq_depth"] == 1
        assert received_events[0].data["queue_url"] == dlq_url
