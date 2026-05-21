"""
Pydantic schemas for SQS task queue messages.

Defines the structured message format for asynchronous provisioning tasks
sent between the API gateway and background workers.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class TaskType(StrEnum):
    """Types of provisioning tasks that can be queued."""

    PROVISION = "provision"
    DEPROVISION = "deprovision"
    UPDATE_CONFIG = "update_config"
    SCALE = "scale"


class TaskMessage(BaseModel):
    """Structured message body for SQS task queue."""

    task_id: str = Field(..., description="Unique task identifier.")
    task_type: TaskType = Field(..., description="Type of provisioning task.")
    resource_id: str = Field(..., description="ID of the resource being operated on.")
    configuration: dict[str, Any] = Field(
        default_factory=dict,
        description="Configuration payload for the task.",
    )
    requested_by: str = Field(default="system")
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    retry_count: int = Field(default=0, ge=0)
    priority: int = Field(
        default=0,
        ge=0,
        le=10,
        description="Task priority (0 = normal, 10 = critical).",
    )


class SQSMessageWrapper(BaseModel):
    """Wrapper around an SQS message with receipt handle for deletion."""

    message_id: str
    receipt_handle: str
    body: TaskMessage
    approximate_receive_count: int = 1
