"""
Pydantic schemas for platform resource state management.

Defines the canonical resource record stored in DynamoDB, including
the state machine for provisioning lifecycle transitions.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class ResourceState(StrEnum):
    """Resource provisioning lifecycle states."""

    PENDING = "PENDING"
    PROVISIONING = "PROVISIONING"
    ACTIVE = "ACTIVE"
    FAILED = "FAILED"
    DEPROVISIONING = "DEPROVISIONING"
    DELETED = "DELETED"


# Valid state transitions
VALID_TRANSITIONS: dict[ResourceState, set[ResourceState]] = {
    ResourceState.PENDING: {ResourceState.PROVISIONING, ResourceState.FAILED},
    ResourceState.PROVISIONING: {ResourceState.ACTIVE, ResourceState.FAILED},
    ResourceState.ACTIVE: {ResourceState.DEPROVISIONING, ResourceState.FAILED},
    ResourceState.FAILED: {ResourceState.PENDING, ResourceState.DELETED},
    ResourceState.DEPROVISIONING: {ResourceState.DELETED, ResourceState.FAILED},
    ResourceState.DELETED: set(),  # Terminal state
}


class ResourceRecord(BaseModel):
    """Canonical resource record stored in DynamoDB.

    Partition key: resource_id
    Sort key: resource_type
    """

    resource_id: str = Field(..., description="Unique resource identifier (ULID).")
    resource_type: str = Field(..., description="Type of resource / action (sort key).")
    state: ResourceState = Field(default=ResourceState.PENDING)
    configuration: dict[str, Any] = Field(
        default_factory=dict,
        description="The applied configuration payload.",
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    created_by: str = Field(default="system")
    version: int = Field(
        default=1,
        ge=1,
        description="Optimistic concurrency version number.",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Arbitrary metadata (original input, target service, etc.).",
    )
    error_message: str | None = Field(
        default=None,
        description="Error details if state is FAILED.",
    )
    actual_state: ResourceState | None = Field(
        default=None,
        description="Actual state of the resource on edge proxy.",
    )
    sync_status: str | None = Field(
        default=None,
        description="Sync status of desired vs actual state (e.g. IN_SYNC, DRIFTED, ABSENT).",
    )

    def can_transition_to(self, new_state: ResourceState) -> bool:
        """Check if transitioning to the new state is valid."""
        return new_state in VALID_TRANSITIONS.get(self.state, set())

    def to_dynamodb_item(self) -> dict[str, Any]:
        """Serialize to a DynamoDB-compatible dict."""
        return {
            "resource_id": self.resource_id,
            "resource_type": self.resource_type,
            "state": self.state.value,
            "configuration": self.configuration,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "created_by": self.created_by,
            "version": self.version,
            "metadata": self.metadata,
            "error_message": self.error_message,
            "actual_state": self.actual_state.value if self.actual_state else None,
            "sync_status": self.sync_status,
        }

    @classmethod
    def from_dynamodb_item(cls, item: dict[str, Any]) -> ResourceRecord:
        """Deserialize from a DynamoDB item."""
        return cls(
            resource_id=item["resource_id"],
            resource_type=item["resource_type"],
            state=ResourceState(item["state"]),
            configuration=item.get("configuration", {}),
            created_at=datetime.fromisoformat(item["created_at"]),
            updated_at=datetime.fromisoformat(item["updated_at"]),
            created_by=item.get("created_by", "system"),
            version=int(item.get("version", 1)),
            metadata=item.get("metadata", {}),
            error_message=item.get("error_message"),
            actual_state=ResourceState(item["actual_state"]) if item.get("actual_state") else None,
            sync_status=item.get("sync_status"),
        )
