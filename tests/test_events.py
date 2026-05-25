"""
Tests for the Event Bus — pub/sub broadcasting and subscriber management.
"""

from __future__ import annotations

import asyncio

import pytest

from broker.services.event_bus import Event, EventBus


class TestEventBus:
    """Tests for the in-process event bus."""

    @pytest.mark.asyncio
    async def test_subscribe_and_receive_event(self):
        """Subscriber should receive published events."""
        bus = EventBus()
        received: list[Event] = []

        async def collector():
            async for event in bus.subscribe("test-client"):
                received.append(event)
                break  # Stop after first event

        # Start collector and publish an event
        task = asyncio.create_task(collector())
        await asyncio.sleep(0.05)  # Let subscriber register

        event = Event(event_type="test", resource_id="res-1", data={"key": "value"})
        await bus.publish(event)

        await asyncio.wait_for(task, timeout=1.0)

        assert len(received) == 1
        assert received[0].event_type == "test"
        assert received[0].resource_id == "res-1"

    @pytest.mark.asyncio
    async def test_multiple_subscribers(self):
        """Multiple subscribers should each receive the event."""
        bus = EventBus()
        received_a: list[Event] = []
        received_b: list[Event] = []

        async def collector_a():
            async for event in bus.subscribe("client-a"):
                received_a.append(event)
                break

        async def collector_b():
            async for event in bus.subscribe("client-b"):
                received_b.append(event)
                break

        task_a = asyncio.create_task(collector_a())
        task_b = asyncio.create_task(collector_b())
        await asyncio.sleep(0.05)

        await bus.publish(Event(event_type="broadcast", resource_id="res-1"))

        await asyncio.wait_for(asyncio.gather(task_a, task_b), timeout=1.0)

        assert len(received_a) == 1
        assert len(received_b) == 1

    @pytest.mark.asyncio
    async def test_subscriber_count(self):
        """Subscriber count should update on subscribe/unsubscribe."""
        bus = EventBus()

        assert bus.subscriber_count == 0

        async def subscriber():
            async for _event in bus.subscribe("test"):
                break

        _task = asyncio.create_task(subscriber())
        await asyncio.sleep(0.05)
        assert bus.subscriber_count == 1

        await bus.unsubscribe("test")
        await asyncio.sleep(0.05)
        assert bus.subscriber_count == 0

    @pytest.mark.asyncio
    async def test_publish_state_change_convenience(self):
        """publish_state_change should create a properly formatted event."""
        bus = EventBus()
        received: list[Event] = []

        async def collector():
            async for event in bus.subscribe("test"):
                received.append(event)
                break

        task = asyncio.create_task(collector())
        await asyncio.sleep(0.05)

        await bus.publish_state_change("res-1", "ACTIVE", extra="data")

        await asyncio.wait_for(task, timeout=1.0)

        assert received[0].event_type == "state_change"
        assert received[0].resource_id == "res-1"
        assert received[0].state == "ACTIVE"
        assert received[0].data["extra"] == "data"

    @pytest.mark.asyncio
    async def test_shutdown_stops_subscribers(self):
        """Shutdown should signal all subscribers to stop."""
        bus = EventBus()
        stopped = False

        async def subscriber():
            nonlocal stopped
            async for _event in bus.subscribe("test"):
                pass
            stopped = True

        task = asyncio.create_task(subscriber())
        await asyncio.sleep(0.05)

        await bus.shutdown()
        await asyncio.wait_for(task, timeout=1.0)

        assert stopped
        assert bus.subscriber_count == 0

    def test_event_to_sse_data(self):
        """Event should serialize to valid JSON string for SSE."""
        event = Event(
            event_type="state_change",
            resource_id="res-1",
            state="ACTIVE",
            data={"key": "value"},
        )

        sse_data = event.to_sse_data()
        assert '"event_type":"state_change"' in sse_data
        assert '"resource_id":"res-1"' in sse_data

    @pytest.mark.asyncio
    async def test_event_bus_metrics_tracking(self):
        """Publishing events should update the event bus metrics counters."""
        bus = EventBus()

        assert bus.metrics["intent_parse_success"] == 0
        assert bus.metrics["intent_parse_failed"] == 0
        assert bus.metrics["provision_success"] == 0
        assert bus.metrics["provision_failed"] == 0

        await bus.publish(Event(event_type="intent_parsed", data={"status": "success"}))
        await bus.publish(Event(event_type="intent_parsed", data={"status": "failed"}))
        assert bus.metrics["intent_parse_success"] == 1
        assert bus.metrics["intent_parse_failed"] == 1

        await bus.publish(Event(event_type="state_change", state="ACTIVE"))
        await bus.publish(Event(event_type="state_change", state="FAILED"))
        assert bus.metrics["provision_success"] == 1
        assert bus.metrics["provision_failed"] == 1

    def test_metrics_api_route(self):
        """GET /api/v1/events/metrics should return the event bus metrics."""
        from fastapi.testclient import TestClient
        from broker.main import app
        from broker.dependencies import get_event_bus
        from broker.services.event_bus import EventBus

        bus = EventBus()
        bus.metrics["intent_parse_success"] = 12
        app.dependency_overrides[get_event_bus] = lambda: bus

        client = TestClient(app)
        response = client.get("/api/v1/events/metrics")
        assert response.status_code == 200
        data = response.json()
        assert data["intent_parse_success"] == 12
        assert "provision_success" in data

        # Clear override
        app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_publish_api_route(self):
        """POST /api/v1/events/publish should publish the event to the event bus."""
        from fastapi.testclient import TestClient
        from broker.main import app
        from broker.dependencies import get_event_bus
        from broker.services.event_bus import EventBus

        bus = EventBus()
        app.dependency_overrides[get_event_bus] = lambda: bus

        client = TestClient(app)
        event_payload = {
            "event_type": "anomaly",
            "resource_id": "test-res",
            "state": "FAILED",
            "data": {"message": "something failed"}
        }

        # Subscribe to see if it publishes
        received_events = []
        async def mock_subscribe():
            async for ev in bus.subscribe("test-sub"):
                received_events.append(ev)
                break

        sub_task = asyncio.create_task(mock_subscribe())

        # Give subscriber a tiny bit of time to register
        await asyncio.sleep(0.01)

        response = client.post("/api/v1/events/publish", json=event_payload)
        assert response.status_code == 200
        assert response.json() == {"status": "published"}

        # Wait for subscriber task to complete
        await asyncio.wait_for(sub_task, timeout=1.0)
        assert len(received_events) == 1
        assert received_events[0].event_type == "anomaly"
        assert received_events[0].resource_id == "test-res"
        assert received_events[0].data["message"] == "something failed"

        app.dependency_overrides.clear()
