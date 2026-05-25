"""
SQS Service — async message queue operations for task management.

Handles enqueueing provisioning tasks and receiving them in the
background worker with long polling and dead-letter queue support.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import structlog

from broker.schemas.task import SQSMessageWrapper, TaskMessage

if TYPE_CHECKING:
    from broker.config import Settings

logger = structlog.get_logger()


class SQSService:
    """Async SQS operations for task queue management."""

    def __init__(self, sqs_client: Any, settings: Settings) -> None:
        self._client = sqs_client
        self._settings = settings
        self._queue_url = settings.sqs_queue_url
        self._dlq_url = settings.sqs_dlq_url

    async def enqueue_task(self, task: TaskMessage) -> str:
        """Send a task message to the SQS queue.

        Args:
            task: The task message to enqueue.

        Returns:
            The SQS message ID.
        """
        response = await self._client.send_message(
            QueueUrl=self._queue_url,
            MessageBody=task.model_dump_json(),
            MessageAttributes={
                "task_type": {
                    "DataType": "String",
                    "StringValue": task.task_type.value,
                },
                "resource_id": {
                    "DataType": "String",
                    "StringValue": task.resource_id,
                },
                **({
                    "correlation_id": {
                        "DataType": "String",
                        "StringValue": task.correlation_id,
                    }
                } if task.correlation_id else {})
            },
        )

        message_id = response["MessageId"]

        await logger.ainfo(
            "Task enqueued to SQS",
            message_id=message_id,
            task_id=task.task_id,
            task_type=task.task_type,
            resource_id=task.resource_id,
        )

        return message_id

    async def receive_tasks(
        self,
        max_messages: int = 10,
        wait_seconds: int = 20,
        visibility_timeout: int = 60,
    ) -> list[SQSMessageWrapper]:
        """Receive task messages from the SQS queue using long polling.

        Args:
            max_messages: Maximum number of messages to receive (1-10).
            wait_seconds: Long polling wait time in seconds (0-20).
            visibility_timeout: Time in seconds before message becomes visible again.

        Returns:
            List of received messages wrapped with receipt handles.
        """
        response = await self._client.receive_message(
            QueueUrl=self._queue_url,
            MaxNumberOfMessages=min(max_messages, 10),
            WaitTimeSeconds=wait_seconds,
            VisibilityTimeout=visibility_timeout,
            MessageAttributeNames=["All"],
            AttributeNames=["ApproximateReceiveCount"],
        )

        messages = response.get("Messages", [])
        wrapped: list[SQSMessageWrapper] = []

        for msg in messages:
            try:
                task = TaskMessage.model_validate_json(msg["Body"])
                # Populate correlation_id from SQS message attributes if not already set in JSON body
                msg_attrs = msg.get("MessageAttributes", {})
                corr_id_attr = msg_attrs.get("correlation_id", {}).get("StringValue")
                if corr_id_attr and not task.correlation_id:
                    task.correlation_id = corr_id_attr

                receive_count = int(msg.get("Attributes", {}).get("ApproximateReceiveCount", 1))

                wrapped.append(
                    SQSMessageWrapper(
                        message_id=msg["MessageId"],
                        receipt_handle=msg["ReceiptHandle"],
                        body=task,
                        approximate_receive_count=receive_count,
                    )
                )
            except Exception as e:
                await logger.aerror(
                    "Failed to parse SQS message",
                    message_id=msg.get("MessageId"),
                    error=str(e),
                )
                # Delete malformed messages to prevent queue poisoning
                await self.delete_task(msg["ReceiptHandle"])

        return wrapped

    async def delete_task(self, receipt_handle: str) -> None:
        """Delete a successfully processed message from the queue.

        Args:
            receipt_handle: The SQS receipt handle for message deletion.
        """
        await self._client.delete_message(
            QueueUrl=self._queue_url,
            ReceiptHandle=receipt_handle,
        )

    async def send_to_dlq(self, message: SQSMessageWrapper, error: str) -> None:
        """Explicitly send a failed message to the dead-letter queue.

        Args:
            message: The failed message.
            error: Description of the failure.
        """
        dlq_body = {
            "original_message": message.body.model_dump(mode="json"),
            "error": error,
            "receive_count": message.approximate_receive_count,
        }

        await self._client.send_message(
            QueueUrl=self._dlq_url,
            MessageBody=json.dumps(dlq_body),
            MessageAttributes={
                "error_type": {
                    "DataType": "String",
                    "StringValue": "processing_failure",
                },
            },
        )

        # Delete from main queue to prevent reprocessing
        await self.delete_task(message.receipt_handle)

        await logger.awarning(
            "Message sent to DLQ",
            task_id=message.body.task_id,
            error=error,
            receive_count=message.approximate_receive_count,
        )

    async def get_queue_depth(self) -> int:
        """Get the approximate number of messages in the queue."""
        response = await self._client.get_queue_attributes(
            QueueUrl=self._queue_url,
            AttributeNames=["ApproximateNumberOfMessages"],
        )
        return int(response["Attributes"]["ApproximateNumberOfMessages"])
