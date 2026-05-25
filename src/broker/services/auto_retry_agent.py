"""
Auto-Retry Agent — LLM-driven self-healing for failed provisioning configurations.
"""

from __future__ import annotations

import structlog
from typing import Any

from broker.schemas.intent import ParsedConfiguration, ValidationResult
from broker.schemas.resource import ResourceState
from broker.schemas.task import TaskMessage, TaskType
from broker.services.dynamodb import DynamoDBService
from broker.services.llm_gateway import LLMGateway
from broker.services.safety import SafetyService
from broker.services.sqs import SQSService

logger = structlog.get_logger()


class AutoRetryAgent:
    """Agent that diagnoses provisioning errors and triggers self-correcting retries."""

    def __init__(
        self,
        db_service: DynamoDBService,
        sqs_service: SQSService,
        llm_gateway: LLMGateway,
        safety_service: SafetyService,
    ) -> None:
        self._db = db_service
        self._sqs = sqs_service
        self._llm = llm_gateway
        self._safety = safety_service

    async def auto_retry_resource(
        self,
        resource_id: str,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        """Diagnose a failed resource's configuration, get a fix from LLM, and trigger a retry task.

        Args:
            resource_id: The ID of the failed resource in DynamoDB.
            correlation_id: Optional correlation trace ID for the retry attempt.

        Returns:
            Dict detailing the diagnostic results and retry status.
        """
        await logger.ainfo("Auto-Retry Agent triggered", resource_id=resource_id)

        # 1. Fetch resource record from DynamoDB
        resource = await self._db.get_resource(resource_id)
        if not resource:
            raise ValueError(f"Resource {resource_id} not found in database.")

        if resource.state != ResourceState.FAILED:
            await logger.awarning(
                "Resource is not in FAILED state, skipping auto-retry",
                resource_id=resource_id,
                current_state=resource.state,
            )
            return {
                "status": "skipped",
                "message": f"Resource is in state {resource.state}, not FAILED.",
            }

        action = resource.resource_type
        parameters = resource.configuration
        error_message = resource.error_message or "Unknown provisioning failure."

        await logger.ainfo(
            "Diagnosing failed configuration",
            resource_id=resource_id,
            action=action,
            error=error_message,
        )

        # 2. Formulate diagnostic prompt for LLM
        prompt = (
            f"Fix the following failed configuration.\n\n"
            f"Action: {action}\n"
            f"Failed Parameters: {parameters}\n"
            f"Error message: {error_message}\n\n"
            f"Please diagnose the error, resolve it in the configuration parameter values, "
            f"and explain your reasoning. Note: The target_service name must remain '{resource.metadata.get('target_service', 'service')}'."
        )

        # Merge environment context if available
        context = {
            "environment": resource.metadata.get("environment", "development"),
            "original_input": resource.metadata.get("original_input", ""),
        }

        # 3. Call LLM Gateway to generate corrected config
        corrected_config: ParsedConfiguration = await self._llm.parse_intent(prompt, context=context)

        await logger.ainfo(
            "LLM returned corrected configuration",
            resource_id=resource_id,
            reasoning=corrected_config.reasoning,
        )

        # 4. Perform safety and compliance checks (including OPA)
        validation: ValidationResult = await self._safety.validate_config(
            corrected_config,
            None,
            context=context,
        )

        if not validation.is_valid:
            await logger.aerror(
                "Corrected configuration failed safety/compliance validation",
                resource_id=resource_id,
                errors=validation.errors,
            )
            return {
                "status": "failed_validation",
                "errors": validation.errors,
                "diagnosed_config": corrected_config.model_dump(),
            }

        # 5. Update resource configuration and reset state to PENDING in DynamoDB
        # We step through resource state transitions to keep optimistic locking version chain intact
        try:
            # First reset state to PENDING and clear error message
            table = await self._db._get_table()
            updated_config = corrected_config.parameters.model_dump(exclude_none=True)
            
            # Perform atomic state change and configuration update
            await table.update_item(
                Key={"resource_id": resource.resource_id, "resource_type": resource.resource_type},
                UpdateExpression=(
                    "SET #state = :state, "
                    "configuration = :config, "
                    "version = version + :inc_one, "
                    "error_message = :null_val, "
                    "updated_at = :now"
                ),
                ExpressionAttributeNames={"#state": "state"},
                ExpressionAttributeValues={
                    ":state": ResourceState.PENDING.value,
                    ":config": updated_config,
                    ":inc_one": 1,
                    ":null_val": None,
                    ":now": self._db._now_iso(),
                },
            )
            await logger.ainfo(
                "Resource state reset to PENDING and configuration updated",
                resource_id=resource_id,
            )
        except Exception as e:
            await logger.aerror(
                "Failed to update resource record during auto-retry initiation",
                error=str(e),
            )
            raise

        # 6. Enqueue a new SQS task to retry the provisioning
        task = TaskMessage(
            task_id=resource_id,
            task_type=TaskType.PROVISION,
            resource_id=resource_id,
            configuration=corrected_config.model_dump(),
            requested_by="auto_retry_agent",
            correlation_id=correlation_id,
        )
        message_id = await self._sqs.enqueue_task(task)

        await logger.ainfo(
            "Auto-retry task successfully enqueued",
            resource_id=resource_id,
            message_id=message_id,
        )

        return {
            "status": "retrying",
            "message_id": message_id,
            "diagnosed_config": corrected_config.model_dump(),
            "reasoning": corrected_config.reasoning,
        }
