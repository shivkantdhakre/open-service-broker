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
