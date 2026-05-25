"""
Event Bus — in-process async pub/sub for real-time event broadcasting.

Supports multiple subscribers via asyncio.Queue. Each subscriber gets
its own queue to decouple consumption rates. Events are published to
all active subscribers simultaneously.

For horizontal scaling, this can be extended with a Redis Pub/Sub adapter.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

logger = structlog.get_logger()


class Event(BaseModel):
    """A real-time event pushed to connected clients."""

    event_type: str = Field(..., description="Type of event (e.g., 'state_change', 'anomaly').")
    resource_id: str | None = Field(default=None, description="Related resource ID.")
    state: str | None = Field(default=None, description="New state (if state change).")
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    data: dict[str, Any] = Field(default_factory=dict, description="Event-specific payload.")

    def to_sse_data(self) -> str:
        """Serialize to SSE-compatible JSON string."""
        return self.model_dump_json()


class EventBus:
    """In-process async pub/sub event bus.

    Each subscriber gets a dedicated asyncio.Queue, ensuring that slow
    consumers don't block event delivery to faster ones.
    """

    def __init__(self, max_queue_size: int = 100) -> None:
        self._subscribers: dict[str, asyncio.Queue[Event | None]] = {}
        self._max_queue_size = max_queue_size
        self._lock = asyncio.Lock()
        self.metrics: dict[str, int] = {
            "intent_parse_success": 0,
            "intent_parse_failed": 0,
            "provision_success": 0,
            "provision_failed": 0,
        }

    async def subscribe(self, client_id: str) -> AsyncIterator[Event]:
        """Subscribe to the event stream.

        Args:
            client_id: Unique identifier for this subscriber.

        Yields:
            Events as they are published.
        """
        queue: asyncio.Queue[Event | None] = asyncio.Queue(maxsize=self._max_queue_size)

        async with self._lock:
            self._subscribers[client_id] = queue

        await logger.ainfo("Client subscribed to event bus", client_id=client_id)

        try:
            while True:
                event = await queue.get()
                if event is None:
                    # Shutdown signal
                    break
                yield event
        finally:
            async with self._lock:
                self._subscribers.pop(client_id, None)
            await logger.ainfo("Client unsubscribed from event bus", client_id=client_id)

    async def unsubscribe(self, client_id: str) -> None:
        """Remove a subscriber from the event bus.

        Args:
            client_id: The subscriber to remove.
        """
        async with self._lock:
            queue = self._subscribers.pop(client_id, None)

        if queue:
            # Send None to signal the subscriber's iterator to stop
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(None)

    async def publish(self, event: Event) -> None:
        """Broadcast an event to all active subscribers.

        If a subscriber's queue is full, the event is dropped for that
        subscriber to prevent backpressure from blocking the publisher.

        Args:
            event: The event to broadcast.
        """
        # Update metrics counters based on event metadata
        if event.event_type == "intent_parsed":
            status = event.data.get("status")
            if status == "success":
                self.metrics["intent_parse_success"] += 1
            elif status == "failed":
                self.metrics["intent_parse_failed"] += 1
        elif event.event_type == "state_change" or event.event_type == "message":
            state = event.state or event.data.get("state")
            if isinstance(state, str):
                state = state.upper()
                if state == "ACTIVE":
                    self.metrics["provision_success"] += 1
                elif state == "FAILED":
                    self.metrics["provision_failed"] += 1

        async with self._lock:
            subscriber_ids = list(self._subscribers.keys())

        dropped = 0
        for client_id in subscriber_ids:
            queue = self._subscribers.get(client_id)
            if queue:
                try:
                    queue.put_nowait(event)
                except asyncio.QueueFull:
                    dropped += 1
                    await logger.awarning(
                        "Event dropped for slow subscriber",
                        client_id=client_id,
                        event_type=event.event_type,
                    )

        if subscriber_ids:
            await logger.adebug(
                "Event published",
                event_type=event.event_type,
                subscriber_count=len(subscriber_ids),
                dropped=dropped,
            )

    async def publish_state_change(
        self,
        resource_id: str,
        new_state: str,
        **extra_data: Any,
    ) -> None:
        """Convenience method to publish a resource state change event."""
        event = Event(
            event_type="state_change",
            resource_id=resource_id,
            state=new_state,
            data=extra_data,
        )
        await self.publish(event)

    @property
    def subscriber_count(self) -> int:
        """Return the current number of active subscribers."""
        return len(self._subscribers)

    async def shutdown(self) -> None:
        """Shutdown the event bus, notifying all subscribers."""
        async with self._lock:
            for queue in self._subscribers.values():
                with contextlib.suppress(asyncio.QueueFull):
                    queue.put_nowait(None)
            self._subscribers.clear()

        await logger.ainfo("Event bus shut down")
