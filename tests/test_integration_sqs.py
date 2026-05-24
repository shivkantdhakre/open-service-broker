"""
Integration tests for the SQS Service.

Verifies:
- Enqueue + receive roundtrip: Send a TaskMessage and receive it.
- Message deletion: Verify deleted messages do not reappear.
- Visbility timeout: Received messages reappear if not deleted.
- DLQ routing: Sending explicitly to DLQ via send_to_dlq, and redrive policy routing.
- Queue depth querying.
"""

from __future__ import annotations

import asyncio
import pytest

from broker.schemas.task import TaskMessage, TaskType
from broker.services.sqs import SQSService


@pytest.mark.asyncio
class TestSQSIntegration:
    """End-to-end integration tests for SQSService."""

    async def test_enqueue_receive_delete_roundtrip(self, async_sqs_service):
        """Verify enqueuing a task, receiving it, and deleting it from the queue."""
        # 1. Verify queue starts empty
        depth = await async_sqs_service.get_queue_depth()
        assert depth == 0

        # 2. Enqueue task
        task = TaskMessage(
            task_id="task-123",
            task_type=TaskType.PROVISION,
            resource_id="res-456",
            configuration={"route_name": "test-route"},
            requested_by="api",
        )
        msg_id = await async_sqs_service.enqueue_task(task)
        assert msg_id is not None

        # Verify queue depth is now 1
        depth = await async_sqs_service.get_queue_depth()
        assert depth == 1

        # 3. Receive task (long poll)
        messages = await async_sqs_service.receive_tasks(max_messages=1, wait_seconds=1)
        assert len(messages) == 1
        received = messages[0]
        assert received.message_id == msg_id
        assert received.body.task_id == "task-123"
        assert received.body.resource_id == "res-456"
        assert received.body.configuration["route_name"] == "test-route"
        assert received.approximate_receive_count == 1
        assert received.receipt_handle is not None

        # 4. Delete task
        await async_sqs_service.delete_task(received.receipt_handle)

        # Verify queue is empty again
        depth = await async_sqs_service.get_queue_depth()
        assert depth == 0

    async def test_message_visibility_timeout(self, async_sqs_service):
        """Verify that a received message reappears in the queue if not deleted after visibility timeout."""
        task = TaskMessage(
            task_id="task-visibility",
            task_type=TaskType.SCALE,
            resource_id="service-abc",
        )
        await async_sqs_service.enqueue_task(task)

        # Receive with a short visibility timeout of 1 second
        messages = await async_sqs_service.receive_tasks(max_messages=1, wait_seconds=1, visibility_timeout=1)
        assert len(messages) == 1

        # Wait 1.5 seconds for visibility timeout to expire
        await asyncio.sleep(1.5)

        # Receive again — it should reappear
        messages_again = await async_sqs_service.receive_tasks(max_messages=1, wait_seconds=1)
        assert len(messages_again) == 1
        assert messages_again[0].body.task_id == "task-visibility"
        assert messages_again[0].approximate_receive_count == 2

        # Clean up
        await async_sqs_service.delete_task(messages_again[0].receipt_handle)

    async def test_send_to_dlq_explicitly(self, async_sqs_service):
        """Verify sending a failed message explicitly to the DLQ."""
        task = TaskMessage(
            task_id="task-fail-dlq",
            task_type=TaskType.DEPROVISION,
            resource_id="res-789",
        )
        await async_sqs_service.enqueue_task(task)

        # Receive
        messages = await async_sqs_service.receive_tasks(max_messages=1, wait_seconds=1)
        assert len(messages) == 1
        msg = messages[0]

        # Send to DLQ
        await async_sqs_service.send_to_dlq(msg, error="Test failure sending to DLQ")

        # Verify main queue is empty
        depth = await async_sqs_service.get_queue_depth()
        assert depth == 0
