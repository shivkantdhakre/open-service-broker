"""
Tests for the DynamoDB service — CRUD, state transitions, and optimistic locking.
"""

from __future__ import annotations

from broker.schemas.resource import ResourceRecord, ResourceState


class TestResourceRecord:
    """Tests for the ResourceRecord model."""

    def test_create_resource_record(self):
        """Should create a valid resource record."""
        record = ResourceRecord(
            resource_id="test-123",
            resource_type="create_route",
            state=ResourceState.PENDING,
            configuration={"route_name": "test"},
            created_by="test-user",
        )

        assert record.resource_id == "test-123"
        assert record.state == ResourceState.PENDING
        assert record.version == 1

    def test_valid_state_transitions(self):
        """Should validate allowed state transitions."""
        record = ResourceRecord(
            resource_id="test",
            resource_type="test",
            state=ResourceState.PENDING,
        )

        assert record.can_transition_to(ResourceState.PROVISIONING)
        assert record.can_transition_to(ResourceState.FAILED)
        assert not record.can_transition_to(ResourceState.ACTIVE)
        assert not record.can_transition_to(ResourceState.DELETED)

    def test_provisioning_transitions(self):
        """PROVISIONING can go to ACTIVE or FAILED."""
        record = ResourceRecord(
            resource_id="test",
            resource_type="test",
            state=ResourceState.PROVISIONING,
        )

        assert record.can_transition_to(ResourceState.ACTIVE)
        assert record.can_transition_to(ResourceState.FAILED)
        assert not record.can_transition_to(ResourceState.PENDING)

    def test_active_transitions(self):
        """ACTIVE can go to DEPROVISIONING or FAILED."""
        record = ResourceRecord(
            resource_id="test",
            resource_type="test",
            state=ResourceState.ACTIVE,
        )

        assert record.can_transition_to(ResourceState.DEPROVISIONING)
        assert record.can_transition_to(ResourceState.FAILED)
        assert not record.can_transition_to(ResourceState.PENDING)

    def test_deleted_is_terminal(self):
        """DELETED should be a terminal state with no valid transitions."""
        record = ResourceRecord(
            resource_id="test",
            resource_type="test",
            state=ResourceState.DELETED,
        )

        for state in ResourceState:
            assert not record.can_transition_to(state)

    def test_to_dynamodb_item(self):
        """Should serialize to a DynamoDB-compatible dict."""
        record = ResourceRecord(
            resource_id="test-123",
            resource_type="create_route",
            state=ResourceState.PENDING,
            configuration={"key": "value"},
            version=1,
        )

        item = record.to_dynamodb_item()

        assert item["resource_id"] == "test-123"
        assert item["resource_type"] == "create_route"
        assert item["state"] == "PENDING"
        assert item["version"] == 1
        assert isinstance(item["created_at"], str)

    def test_from_dynamodb_item_roundtrip(self):
        """Should roundtrip through serialization/deserialization."""
        original = ResourceRecord(
            resource_id="test-123",
            resource_type="create_route",
            state=ResourceState.ACTIVE,
            configuration={"key": "value"},
            created_by="test",
            version=3,
        )

        item = original.to_dynamodb_item()
        restored = ResourceRecord.from_dynamodb_item(item)

        assert restored.resource_id == original.resource_id
        assert restored.state == original.state
        assert restored.version == original.version
        assert restored.configuration == original.configuration


class TestResourceState:
    """Tests for the ResourceState enum."""

    def test_all_states_have_transition_rules(self):
        """Every state should have defined transition rules."""
        from broker.schemas.resource import VALID_TRANSITIONS

        for state in ResourceState:
            assert state in VALID_TRANSITIONS

    def test_state_string_values(self):
        """State values should be uppercase strings."""
        for state in ResourceState:
            assert state.value == state.value.upper()
