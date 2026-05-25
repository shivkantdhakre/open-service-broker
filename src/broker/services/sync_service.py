"""
Sovereign Sync Service — compares desired state (DynamoDB) with actual state (Sovereign).

Enables proxy drift detection and self-healing reporting.
"""

from __future__ import annotations

from typing import Any

import structlog
from pydantic import BaseModel

from broker.schemas.resource import ResourceRecord, ResourceState
from broker.services.dynamodb import DynamoDBService
from broker.services.sovereign_client import SovereignClient
from broker.worker import Worker  # For _parse_target_config helper

logger = structlog.get_logger()


class SovereignSyncService:
    """Service to detect configuration drift and actual proxy state discrepancies."""

    def __init__(
        self,
        dynamodb_service: DynamoDBService,
        sovereign_client: SovereignClient,
    ) -> None:
        self._db = dynamodb_service
        self._sovereign = sovereign_client
        self._worker_helper = Worker()

    async def sync_all_resources(self) -> list[ResourceRecord]:
        """Perform desired-actual state synchronization for all ACTIVE resources."""
        await logger.ainfo("Starting state synchronization check")

        # Get all desired ACTIVE resources
        active_resources = await self._db.list_resources(state=ResourceState.ACTIVE)
        updated_records: list[ResourceRecord] = []

        for record in active_resources:
            updated = await self.sync_resource(record)
            if updated:
                updated_records.append(updated)

        await logger.ainfo(
            "State synchronization completed",
            total_checked=len(active_resources),
            total_updated=len(updated_records),
        )
        return updated_records

    async def sync_resource(self, record: ResourceRecord) -> ResourceRecord | None:
        """Check status of a single resource and update its actual state and sync status."""
        action = record.configuration.get("action")
        # Backwards compatibility: fallback to mapping type if action not nested
        if not action:
            action = record.resource_type

        parameters = record.configuration.get("parameters", record.configuration)

        target_config = self._worker_helper._parse_target_config(
            action=action,
            parameters=parameters,
            resource_id=record.resource_id,
        )

        if not target_config:
            # Cannot match non-xDS resources
            return None

        # Fetch actual config from Sovereign
        actual_raw = await self._fetch_actual_config(action, target_config)

        # Determine states
        new_sync_status = "ABSENT"
        new_actual_state = ResourceState.DELETED

        if actual_raw:
            new_actual_state = ResourceState.ACTIVE
            target_json = target_config.model_dump(mode="json")
            if target_json == actual_raw:
                new_sync_status = "IN_SYNC"
            else:
                new_sync_status = "DRIFTED"
                await logger.awarning(
                    "Configuration drift detected on Sovereign",
                    resource_id=record.resource_id,
                    expected=target_json,
                    actual=actual_raw,
                )

        # Update if changed
        if (
            record.sync_status != new_sync_status
            or record.actual_state != new_actual_state
        ):
            record.sync_status = new_sync_status
            record.actual_state = new_actual_state
            
            # Save changes to DynamoDB by updating the item
            table = await self._db._get_table()
            await table.update_item(
                Key={"resource_id": record.resource_id, "resource_type": record.resource_type},
                UpdateExpression="SET actual_state = :actual, sync_status = :sync",
                ExpressionAttributeValues={
                    ":actual": new_actual_state.value if new_actual_state else None,
                    ":sync": new_sync_status,
                },
            )
            await logger.ainfo(
                "Resource sync status updated in DynamoDB",
                resource_id=record.resource_id,
                sync_status=new_sync_status,
                actual_state=new_actual_state,
            )
            return record

        return None

    async def _fetch_actual_config(self, action: str, target_config: Any) -> dict[str, Any] | None:
        """Helper to fetch config from Sovereign based on action type."""
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
                "Failed to fetch actual configuration from Sovereign during sync",
                action=action,
                error=str(e),
            )
        return None
