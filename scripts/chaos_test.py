"""
Chaos Testing Script — simulates worker failure, SQS DLQ routing, and EventBus anomaly alerts.
"""

from __future__ import annotations

import asyncio
import aioboto3
import json
import logging
import sys

from broker.config import get_settings
from broker.schemas.resource import ResourceRecord, ResourceState
from broker.schemas.task import TaskMessage, TaskType
from broker.services.dynamodb import DynamoDBService
from broker.services.sqs import SQSService
from broker.worker import Worker

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("chaos-test")


async def run_chaos_test():
    settings = get_settings()
    logger.info("Initializing chaos test against SQS & DynamoDB...")

    session = aioboto3.Session(
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
        region_name=settings.aws_region,
    )

    async with session.client("sqs", endpoint_url=settings.aws_endpoint_url) as sqs_client, \
               session.resource("dynamodb", endpoint_url=settings.aws_endpoint_url) as dynamodb:

        db = DynamoDBService(dynamodb, settings)
        sqs_service = SQSService(sqs_client, settings)

        # 1. Setup resource structure in DynamoDB
        logger.info("Setting up resources...")
        try:
            record = ResourceRecord(
                resource_id="chaos-resource-id",
                resource_type="create_route",
                state=ResourceState.PENDING,
                configuration={"action": "create_route", "target_service": "invalid-service-name; DROP TABLE;--"},
            )
            await db.create_resource(record)
            logger.info("DynamoDB resource record created in PENDING state.")
        except Exception as e:
            logger.warning(f"Failed to create DynamoDB record (it might already exist): {e}")

        # 2. Enqueue the invalid task message
        task = TaskMessage(
            task_id="chaos-task-id",
            task_type=TaskType.PROVISION,
            resource_id="chaos-resource-id",
            configuration={"action": "create_route", "target_service": "invalid-service-name; DROP TABLE;--"},
            correlation_id="chaos-correlation-999",
        )
        
        msg_id = await sqs_service.enqueue_task(task)
        logger.info(f"Task enqueued to SQS. Message ID: {msg_id}")

        # 3. Instantiate Worker and trigger message processing (simulating 3rd retry failure)
        logger.info("Instantiating worker to process task...")
        worker = Worker()
        
        # Long-poll and receive the task from SQS
        messages = await sqs_service.receive_tasks(max_messages=1, wait_seconds=1)
        if not messages:
            logger.error("No messages received from SQS queue. Make sure LocalStack is running.")
            sys.exit(1)
            
        msg = messages[0]
        logger.info(f"Worker received message. Task ID: {msg.body.task_id}")
        
        # Force receive count to 3 to trigger immediate DLQ routing upon failure
        msg.approximate_receive_count = 3
        
        # Patch the Sovereign client to throw connection errors to simulate control plane failure
        from unittest.mock import AsyncMock
        from broker.services.sovereign_client import SovereignClient
        mock_sovereign = AsyncMock(spec=SovereignClient)
        mock_sovereign.get_current_config.side_effect = ValueError("Control plane connection failed")
        mock_sovereign.get_route.side_effect = ValueError("Control plane connection failed")
        mock_sovereign.apply_route.side_effect = ValueError("Control plane connection failed")
        worker._sovereign = mock_sovereign

        # 4. Process task and observe routing
        logger.info("Running task processing simulation...")
        try:
            await worker._process_message(sqs_service, msg)
        except Exception as ex:
            logger.info(f"Expected processing exception caught: {ex}")

        # 5. Verify the task exists in the DLQ
        logger.info("Verifying DLQ entry...")
        resp = await sqs_client.receive_message(
            QueueUrl=settings.sqs_dlq_url,
            MaxNumberOfMessages=1,
            WaitTimeSeconds=1
        )
        dlq_messages = resp.get("Messages", [])
        if dlq_messages:
            dlq_msg = dlq_messages[0]
            body = json.loads(dlq_msg["Body"])
            logger.info(f"✔ SUCCESS: Message found in DLQ! Task ID in DLQ: {body.get('task_id')}")
            # Clean up DLQ
            await sqs_client.delete_message(QueueUrl=settings.sqs_dlq_url, ReceiptHandle=dlq_msg["ReceiptHandle"])
        else:
            logger.error("❌ FAILURE: Message was not found in DLQ.")

        # 6. Verify resource state in DB is FAILED
        logger.info("Verifying DynamoDB state updates...")
        updated_resource = await db.get_resource("chaos-resource-id", "create_route")
        if updated_resource and updated_resource.state == ResourceState.FAILED:
            logger.info(f"✔ SUCCESS: Resource state successfully updated to FAILED in DB! Error logged: '{updated_resource.error_message}'")
        else:
            logger.error(f"❌ FAILURE: Resource state in DB is {updated_resource.state if updated_resource else 'None'} (expected FAILED).")


if __name__ == "__main__":
    asyncio.run(run_chaos_test())
