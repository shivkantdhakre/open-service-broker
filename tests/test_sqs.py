"""
Tests for the SQS service — message serialization and task schema validation.
"""

from __future__ import annotations

import pytest

from broker.schemas.task import SQSMessageWrapper, TaskMessage, TaskType


class TestTaskMessage:
    """Tests for the TaskMessage schema."""

    def test_create_task_message(self):
        """Should create a valid task message."""
        task = TaskMessage(
            task_id="task-001",
            task_type=TaskType.PROVISION,
            resource_id="res-123",
            configuration={"key": "value"},
            requested_by="test-user",
        )

        assert task.task_id == "task-001"
        assert task.task_type == TaskType.PROVISION
        assert task.retry_count == 0
        assert task.priority == 0

    def test_task_message_serialization(self):
        """Should serialize to and from JSON."""
        task = TaskMessage(
            task_id="task-001",
            task_type=TaskType.DEPROVISION,
            resource_id="res-123",
            configuration={"route_name": "test"},
        )

        json_str = task.model_dump_json()
        restored = TaskMessage.model_validate_json(json_str)

        assert restored.task_id == task.task_id
        assert restored.task_type == task.task_type
        assert restored.resource_id == task.resource_id
        assert restored.configuration == task.configuration

    def test_all_task_types(self):
        """All TaskType values should be valid."""
        for task_type in TaskType:
            task = TaskMessage(
                task_id="test",
                task_type=task_type,
                resource_id="res",
            )
            assert task.task_type == task_type

    def test_priority_bounds(self):
        """Priority should be constrained to 0-10."""
        task = TaskMessage(
            task_id="test",
            task_type=TaskType.PROVISION,
            resource_id="res",
            priority=10,
        )
        assert task.priority == 10

        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            TaskMessage(
                task_id="test",
                task_type=TaskType.PROVISION,
                resource_id="res",
                priority=11,
            )


class TestSQSMessageWrapper:
    """Tests for the SQS message wrapper."""

    def test_create_wrapper(self):
        """Should wrap a task message with SQS metadata."""
        task = TaskMessage(
            task_id="task-001",
            task_type=TaskType.PROVISION,
            resource_id="res-123",
        )

        wrapper = SQSMessageWrapper(
            message_id="msg-001",
            receipt_handle="handle-abc",
            body=task,
            approximate_receive_count=1,
        )

        assert wrapper.message_id == "msg-001"
        assert wrapper.body.task_id == "task-001"
        assert wrapper.approximate_receive_count == 1
