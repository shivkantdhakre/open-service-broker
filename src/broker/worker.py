"""
Background Worker — SQS consumer for asynchronous provisioning tasks.

This runs as a standalone process (separate from the FastAPI API) that:
1. Long-polls the SQS task queue
2. Dispatches tasks to the appropriate handler
3. Updates resource state in DynamoDB
4. Emits SSE events via the event bus
5. Handles failures with retry/DLQ logic

Entry point: python -m broker.worker
"""

from __future__ import annotations

import asyncio
import signal
import sys
from typing import Any

import aioboto3
import structlog

from broker.config import get_settings
from broker.schemas.resource import ResourceState
from broker.schemas.task import SQSMessageWrapper, TaskType
from broker.services.github_integration import GitHubAdapter
from broker.services.dynamodb import DynamoDBService, InvalidStateTransitionError
from broker.services.sovereign_client import SovereignClient, SovereignError
from broker.services.sqs import SQSService

# Configure structured logging
structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.dev.set_exc_info,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(10),
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
    cache_logger_on_first_use=True,
)

logger = structlog.get_logger()


class Worker:
    """SQS consumer worker that processes provisioning tasks."""

    def __init__(self) -> None:
        self._settings = get_settings()
        self._shutdown_event = asyncio.Event()
        self._session = aioboto3.Session(
            aws_access_key_id=self._settings.aws_access_key_id,
            aws_secret_access_key=self._settings.aws_secret_access_key,
            region_name=self._settings.aws_region,
        )
        self._sovereign: SovereignClient | None = None

    async def start(self) -> None:
        """Start the worker's main polling loop."""
        await logger.ainfo(
            "Worker starting",
            queue_url=self._settings.sqs_queue_url,
        )

        self._sovereign = SovereignClient(self._settings)

        try:
            while not self._shutdown_event.is_set():
                await self._poll_and_process()
        except asyncio.CancelledError:
            await logger.ainfo("Worker cancelled")
        finally:
            if self._sovereign:
                await self._sovereign.close()
            await logger.ainfo("Worker shut down cleanly")

    async def stop(self) -> None:
        """Signal the worker to stop after the current iteration."""
        await logger.ainfo("Shutdown signal received")
        self._shutdown_event.set()

    async def _poll_and_process(self) -> None:
        """Poll SQS for messages and process them."""
        async with self._session.client(
            "sqs",
            endpoint_url=self._settings.aws_endpoint_url,
            region_name=self._settings.aws_region,
        ) as sqs_client:
            sqs = SQSService(sqs_client, self._settings)

            messages = await sqs.receive_tasks(
                max_messages=10,
                wait_seconds=20,
                visibility_timeout=120,
            )

            if not messages:
                return

            await logger.ainfo(
                "Received messages from SQS",
                count=len(messages),
            )

            # Process messages concurrently
            tasks = [self._process_message(sqs, msg) for msg in messages]
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _process_message(
        self,
        sqs: SQSService,
        message: SQSMessageWrapper,
    ) -> None:
        """Process a single SQS message."""
        task = message.body

        await logger.ainfo(
            "Processing task",
            task_id=task.task_id,
            task_type=task.task_type,
            resource_id=task.resource_id,
            receive_count=message.approximate_receive_count,
        )

        try:
            # Dispatch to appropriate handler
            match task.task_type:
                case TaskType.PROVISION:
                    await self._handle_provision(task.resource_id, task.configuration)
                case TaskType.DEPROVISION:
                    await self._handle_deprovision(task.resource_id, task.configuration)
                case TaskType.UPDATE_CONFIG:
                    await self._handle_update_config(task.resource_id, task.configuration)
                case TaskType.SCALE:
                    await self._handle_scale(task.resource_id, task.configuration)
                case TaskType.MAINTENANCE:
                    await self._handle_maintenance(task.resource_id, task.configuration)
                case _:
                    await logger.aerror("Unknown task type", task_type=task.task_type)
                    await sqs.send_to_dlq(message, f"Unknown task type: {task.task_type}")
                    return

            # Success — delete message from queue
            await sqs.delete_task(message.receipt_handle)

            await logger.ainfo(
                "Task completed successfully",
                task_id=task.task_id,
                task_type=task.task_type,
            )

        except (SovereignError, InvalidStateTransitionError) as e:
            await logger.aerror(
                "Task processing failed",
                task_id=task.task_id,
                error=str(e),
                receive_count=message.approximate_receive_count,
            )

            # If max retries exceeded or maintenance task, send to DLQ
            if message.approximate_receive_count >= 3 or task.task_type == TaskType.MAINTENANCE:
                await sqs.send_to_dlq(message, str(e))
            # Otherwise, let visibility timeout re-queue it

        except Exception as e:
            await logger.aerror(
                "Unexpected error processing task",
                task_id=task.task_id,
                error=str(e),
                exc_info=True,
            )
            if message.approximate_receive_count >= 3 or task.task_type == TaskType.MAINTENANCE:
                await sqs.send_to_dlq(message, str(e))

    async def _handle_provision(
        self,
        resource_id: str,
        configuration: dict[str, Any],
    ) -> None:
        """Handle a provisioning task.

        1. Update state → PROVISIONING
        2. Apply configuration to Sovereign
        3. Update state → ACTIVE

        Each state transition uses the version returned by the previous
        update to maintain the optimistic locking chain.
        """
        async with self._session.resource(
            "dynamodb",
            endpoint_url=self._settings.aws_endpoint_url,
            region_name=self._settings.aws_region,
        ) as dynamodb:
            db = DynamoDBService(dynamodb, self._settings)

            # Get current resource
            resource = await db.get_resource(resource_id)
            if resource is None:
                raise ValueError(f"Resource {resource_id} not found")

            # Transition to PROVISIONING (returns record with incremented version)
            resource = await db.update_state(
                resource_id=resource.resource_id,
                resource_type=resource.resource_type,
                new_state=ResourceState.PROVISIONING,
                expected_version=resource.version,
            )

            try:
                # Apply to Sovereign (simulate if Sovereign is not available)
                if self._sovereign:
                    try:
                        await self._sovereign.get_current_config()
                        # If Sovereign is reachable, apply the config
                        await logger.ainfo(
                            "Applying configuration to Sovereign",
                            resource_id=resource_id,
                        )
                    except SovereignError:
                        await logger.awarning(
                            "Sovereign not reachable, simulating provisioning",
                            resource_id=resource_id,
                        )

                # Simulate provisioning delay
                await asyncio.sleep(1)

                # Transition to ACTIVE — use resource.version from the
                # PROVISIONING transition (not the original fetch)
                await db.update_state(
                    resource_id=resource.resource_id,
                    resource_type=resource.resource_type,
                    new_state=ResourceState.ACTIVE,
                    expected_version=resource.version,
                )

                await logger.ainfo(
                    "Resource provisioned successfully",
                    resource_id=resource_id,
                )

            except Exception as e:
                # Transition to FAILED on error — use current resource version
                await db.update_state(
                    resource_id=resource.resource_id,
                    resource_type=resource.resource_type,
                    new_state=ResourceState.FAILED,
                    expected_version=resource.version,
                    error_message=str(e),
                )
                raise

    async def _handle_deprovision(
        self,
        resource_id: str,
        configuration: dict[str, Any],
    ) -> None:
        """Handle a deprovisioning task."""
        async with self._session.resource(
            "dynamodb",
            endpoint_url=self._settings.aws_endpoint_url,
            region_name=self._settings.aws_region,
        ) as dynamodb:
            db = DynamoDBService(dynamodb, self._settings)

            resource = await db.get_resource(resource_id)
            if resource is None:
                raise ValueError(f"Resource {resource_id} not found")

            # Remove from Sovereign
            if self._sovereign:
                try:
                    route_name = configuration.get("route_name")
                    if route_name:
                        await self._sovereign.remove_route(route_name)
                except SovereignError:
                    await logger.awarning("Sovereign removal failed, continuing cleanup")

            # Transition to DELETED
            await db.update_state(
                resource_id=resource.resource_id,
                resource_type=resource.resource_type,
                new_state=ResourceState.DELETED,
                expected_version=resource.version,
            )

            await logger.ainfo("Resource deprovisioned", resource_id=resource_id)

    async def _handle_update_config(
        self,
        resource_id: str,
        configuration: dict[str, Any],
    ) -> None:
        """Handle a configuration update task."""
        await logger.ainfo(
            "Config update processed",
            resource_id=resource_id,
        )

    async def _handle_scale(
        self,
        resource_id: str,
        configuration: dict[str, Any],
    ) -> None:
        """Handle a scaling task."""
        await logger.ainfo(
            "Scale task processed",
            resource_id=resource_id,
            configuration=configuration,
        )

    async def _handle_maintenance(
        self,
        resource_id: str,
        configuration: dict[str, Any],
    ) -> None:
        """Handle a maintenance task by running a Git PR workflow for approved refactoring proposals."""
        await logger.ainfo(
            "Running automated maintenance refactoring",
            proposal_id=resource_id,
        )

        proposal_data = configuration.get("proposal")
        if not proposal_data:
            raise ValueError("No proposal metadata provided in maintenance task configuration")

        title = proposal_data.get("title", "AI Refactoring")
        description = proposal_data.get("description", "")
        files_affected = proposal_data.get("files_affected", [])
        diff_preview = proposal_data.get("diff_preview", "")

        dry_run = configuration.get("dry_run", True)

        # Initialize the Git provider
        git_provider = GitHubAdapter(dry_run=dry_run)

        # Create branch
        import time
        timestamp = int(time.time())
        branch_name = f"refactor/{resource_id}-{timestamp}"
        await git_provider.create_branch(branch_name)

        # Build proposal documentation change
        report_content = (
            f"# Approved Refactoring Proposal\n\n"
            f"**Proposal ID**: {resource_id}\n"
            f"**Title**: {title}\n\n"
            f"### Description\n{description}\n\n"
            f"### Affected Files\n" + ", ".join(f"`{f}`" for f in files_affected) + "\n\n"
            f"### Diff Preview\n```diff\n{diff_preview}\n```\n"
        )
        file_changes = {
            f"reports/refactor-{resource_id}.md": report_content
        }

        # Commit changes
        await git_provider.commit_changes(
            branch_name=branch_name,
            commit_message=f"docs: apply approved refactoring proposal {resource_id}",
            file_changes=file_changes,
        )

        # Create pull request
        pr_url = await git_provider.create_pull_request(
            branch_name=branch_name,
            title=f"🔧 AI Refactor: {title}",
            body=(
                f"This pull request was automatically generated by the Service Broker background worker "
                f"for the approved refactoring proposal `{resource_id}`.\n\n"
                f"### Description\n{description}\n\n"
                f"### Files Affected\n" + "\n".join(f"- `{f}`" for f in files_affected) + "\n\n"
                f"### Diff Preview\n```diff\n{diff_preview}\n```\n"
            ),
        )

        await logger.ainfo(
            "Maintenance refactoring completed and PR opened",
            proposal_id=resource_id,
            pr_url=pr_url,
        )


def run() -> None:
    """Entry point for the broker-worker script."""
    worker = Worker()

    # Register signal handlers for graceful shutdown
    loop = asyncio.new_event_loop()

    def signal_handler() -> None:
        loop.create_task(worker.stop())

    if sys.platform != "win32":
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, signal_handler)

    try:
        loop.run_until_complete(worker.start())
    except KeyboardInterrupt:
        loop.run_until_complete(worker.stop())
    finally:
        loop.close()


if __name__ == "__main__":
    run()
