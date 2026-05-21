"""
Tests for the background worker — task dispatch and state transitions.
"""

from __future__ import annotations

from broker.schemas.task import TaskMessage, TaskType


class TestWorkerTaskDispatch:
    """Tests for worker task routing and schema validation."""

    def test_provision_task_creation(self):
        """Provision tasks should be created with correct type."""
        task = TaskMessage(
            task_id="task-001",
            task_type=TaskType.PROVISION,
            resource_id="res-123",
            configuration={"action": "create_route", "target_service": "test"},
            requested_by="api",
        )

        assert task.task_type == TaskType.PROVISION
        assert task.resource_id == "res-123"

    def test_deprovision_task_creation(self):
        """Deprovision tasks should be created with correct type."""
        task = TaskMessage(
            task_id="task-002",
            task_type=TaskType.DEPROVISION,
            resource_id="res-123",
            requested_by="api",
        )

        assert task.task_type == TaskType.DEPROVISION

    def test_scale_task_with_configuration(self):
        """Scale tasks should include scaling configuration."""
        task = TaskMessage(
            task_id="task-003",
            task_type=TaskType.SCALE,
            resource_id="service-abc",
            configuration={
                "action": "SCALE_UP",
                "predicted_load": 5000,
                "current_load": 2000,
                "confidence": 0.92,
            },
            requested_by="auto-scaler",
        )

        assert task.task_type == TaskType.SCALE
        assert task.configuration["predicted_load"] == 5000
        assert task.requested_by == "auto-scaler"

    def test_update_config_task(self):
        """Config update tasks should carry the new configuration."""
        task = TaskMessage(
            task_id="task-004",
            task_type=TaskType.UPDATE_CONFIG,
            resource_id="res-456",
            configuration={"lb_policy": "ROUND_ROBIN"},
        )

        assert task.task_type == TaskType.UPDATE_CONFIG
        assert task.configuration["lb_policy"] == "ROUND_ROBIN"
