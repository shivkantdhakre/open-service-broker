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
import httpx
import structlog

from broker.config import get_settings
from broker.schemas.resource import ResourceState
from broker.schemas.sovereign import (
    CircuitBreaker,
    ClusterConfig,
    Endpoint,
    HealthCheck,
    RateLimitConfig,
    RateLimitDescriptor,
    RouteConfig,
    RouteMatch,
    WeightedCluster,
)
from broker.schemas.task import SQSMessageWrapper, TaskType
from broker.services.dynamodb import DynamoDBService, InvalidStateTransitionError
from broker.services.event_bus import Event
from broker.services.github_integration import GitHubAdapter
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

    async def _publish_event(self, event: Event) -> None:
        """Publish an event to the API server's event bus."""
        url = f"http://127.0.0.1:{self._settings.app_port}/api/v1/events/publish"
        headers = {"Content-Type": "application/json"}
        if self._settings.api_keys:
            # Use the first API key from Settings.api_keys to authenticate
            api_key = next(iter(self._settings.api_keys.keys()))
            headers["X-API-Key"] = api_key

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    url,
                    json=event.model_dump(mode="json"),
                    headers=headers,
                )
                resp.raise_for_status()
        except Exception as e:
            await logger.awarning(
                "Failed to publish event to API server",
                url=url,
                error=str(e),
            )

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

    async def _send_to_dlq_and_alert(
        self,
        sqs: SQSService,
        message: SQSMessageWrapper,
        error_msg: str,
    ) -> None:
        """Send a failed message to DLQ and publish an anomaly event."""
        task = message.body
        await sqs.send_to_dlq(message, error_msg)

        anomaly_event = Event(
            event_type="anomaly",
            resource_id=task.resource_id,
            state="FAILED",
            data={
                "message": f"Task {task.task_id} failed and was routed to DLQ: {error_msg}",
                "task_id": task.task_id,
                "task_type": task.task_type,
                "error": error_msg,
                "receive_count": message.approximate_receive_count,
                "correlation_id": task.correlation_id,
            },
        )
        await self._publish_event(anomaly_event)

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
                    await self._send_to_dlq_and_alert(sqs, message, f"Unknown task type: {task.task_type}")
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
                await self._send_to_dlq_and_alert(sqs, message, str(e))
            # Otherwise, let visibility timeout re-queue it

        except Exception as e:
            await logger.aerror(
                "Unexpected error processing task",
                task_id=task.task_id,
                error=str(e),
                exc_info=True,
            )
            if message.approximate_receive_count >= 3 or task.task_type == TaskType.MAINTENANCE:
                await self._send_to_dlq_and_alert(sqs, message, str(e))

    def _parse_target_config(self, action: str, parameters: dict[str, Any], resource_id: str) -> Any:
        """Parse configuration parameters into the corresponding Sovereign config schema."""
        action = action.lower()
        if action in ("create_route", "update_route"):
            route_name = parameters.get("route_name") or resource_id

            # Weighted clusters mapping
            weighted_clusters = None
            wc_raw = parameters.get("weighted_clusters")
            if wc_raw:
                weighted_clusters = [
                    WeightedCluster(
                        cluster_name=wc.get("cluster_name"),
                        weight=wc.get("weight"),
                    ) for wc in wc_raw
                ]

            match_config = RouteMatch(
                prefix=parameters.get("prefix") or "/",
                headers=parameters.get("headers") or {},
                query_parameters=parameters.get("query_parameters") or {},
            )

            return RouteConfig(
                route_name=route_name,
                match=match_config,
                target_cluster=parameters.get("target_cluster"),
                weighted_clusters=weighted_clusters,
                timeout_ms=parameters.get("timeout_ms") or 15000,
                retry_on=parameters.get("retry_on"),
                max_retries=parameters.get("max_retries") or 1,
            )

        elif action in ("create_cluster", "update_cluster"):
            cluster_name = parameters.get("cluster_name") or resource_id

            endpoints = []
            endpoints_raw = parameters.get("endpoints") or []
            for ep in endpoints_raw:
                endpoints.append(Endpoint(
                    address=ep.get("address"),
                    port=ep.get("port"),
                    health_check_port=ep.get("health_check_port"),
                    weight=ep.get("weight") or 1,
                ))

            hc_raw = parameters.get("health_check")
            health_check = None
            if hc_raw:
                health_check = HealthCheck(
                    check_type=hc_raw.get("check_type") or "HTTP",
                    path=hc_raw.get("path") or "/health",
                    interval_ms=hc_raw.get("interval_ms") or 5000,
                    timeout_ms=hc_raw.get("timeout_ms") or 3000,
                    unhealthy_threshold=hc_raw.get("unhealthy_threshold") or 3,
                    healthy_threshold=hc_raw.get("healthy_threshold") or 2,
                )

            cb_raw = parameters.get("circuit_breaker")
            circuit_breaker = None
            if cb_raw:
                circuit_breaker = CircuitBreaker(
                    max_connections=cb_raw.get("max_connections") or 1024,
                    max_pending_requests=cb_raw.get("max_pending_requests") or 1024,
                    max_requests=cb_raw.get("max_requests") or 1024,
                    max_retries=cb_raw.get("max_retries") or 3,
                )

            return ClusterConfig(
                cluster_name=cluster_name,
                lb_policy=parameters.get("lb_policy") or "ROUND_ROBIN",
                endpoints=endpoints,
                health_check=health_check,
                circuit_breaker=circuit_breaker,
                connect_timeout_ms=parameters.get("connect_timeout_ms") or 5000,
            )

        elif action == "update_rate_limit":
            name = parameters.get("name") or resource_id

            descriptors = []
            descriptors_raw = parameters.get("descriptors") or []
            for desc in descriptors_raw:
                descriptors.append(RateLimitDescriptor(
                    key=desc.get("key"),
                    value=desc.get("value"),
                ))

            return RateLimitConfig(
                name=name,
                target_route=parameters.get("target_route") or "",
                requests_per_unit=parameters.get("requests_per_unit") or 1,
                unit=parameters.get("unit") or "minute",
                descriptors=descriptors,
                shadow_mode=parameters.get("shadow_mode") or False,
            )

        return None

    async def _get_existing_config(self, action: str, target_config: Any) -> dict[str, Any] | None:
        """Fetch existing config from Sovereign based on config type."""
        if not self._sovereign:
            return None

        action = action.lower()
        try:
            if action in ("create_route", "update_route"):
                return await self._sovereign.get_route(target_config.route_name)
            elif action in ("create_cluster", "update_cluster"):
                return await self._sovereign.get_cluster(target_config.cluster_name)
            elif action == "update_rate_limit":
                return await self._sovereign.get_rate_limit(target_config.name)
        except Exception as e:
            await logger.awarning(
                "Error checking existing Sovereign configuration",
                error=str(e),
            )
        return None

    async def _apply_config_to_sovereign(self, action: str, target_config: Any) -> None:
        """Apply target config to Sovereign."""
        if not self._sovereign:
            return

        action = action.lower()
        if action in ("create_route", "update_route"):
            await self._sovereign.apply_route(target_config)
        elif action in ("create_cluster", "update_cluster"):
            await self._sovereign.apply_cluster(target_config)
        elif action == "update_rate_limit":
            await self._sovereign.apply_rate_limit(target_config)

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

            # Parse target config and query Sovereign to see if it matches
            action = configuration.get("action")
            parameters = configuration.get("parameters", {})

            target_config = None
            if action:
                target_config = self._parse_target_config(action, parameters, resource_id)

            if target_config and self._sovereign:
                existing_raw = await self._get_existing_config(action, target_config)
                if existing_raw and target_config.model_dump(mode="json") == existing_raw:
                    # Idempotency match! Self-heal.
                    await logger.ainfo(
                        "Idempotency match: configuration already exists and matches in Sovereign. Skipping write and self-healing state in DB.",
                        resource_id=resource_id,
                        action=action,
                    )

                    # Transition directly to ACTIVE using state machine path
                    if resource.state == ResourceState.FAILED:
                        resource = await db.update_state(
                            resource_id=resource.resource_id,
                            resource_type=resource.resource_type,
                            new_state=ResourceState.PENDING,
                            expected_version=resource.version,
                        )
                    if resource.state == ResourceState.PENDING:
                        resource = await db.update_state(
                            resource_id=resource.resource_id,
                            resource_type=resource.resource_type,
                            new_state=ResourceState.PROVISIONING,
                            expected_version=resource.version,
                        )
                    if resource.state == ResourceState.PROVISIONING:
                        await db.update_state(
                            resource_id=resource.resource_id,
                            resource_type=resource.resource_type,
                            new_state=ResourceState.ACTIVE,
                            expected_version=resource.version,
                        )
                    return

            # Transition to PROVISIONING (returns record with incremented version)
            resource = await db.update_state(
                resource_id=resource.resource_id,
                resource_type=resource.resource_type,
                new_state=ResourceState.PROVISIONING,
                expected_version=resource.version,
            )

            try:
                # Apply to Sovereign
                if self._sovereign and target_config:
                    await logger.ainfo(
                        "Applying configuration to Sovereign",
                        resource_id=resource_id,
                        action=action,
                    )
                    await self._apply_config_to_sovereign(action, target_config)

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
    loop = asyncio.new_event_loop()

    # Keep a reference to tasks to prevent garbage collection mid-execution
    background_tasks: set[asyncio.Task[Any]] = set()

    def signal_handler() -> None:
        task = loop.create_task(worker.stop())
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)

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
