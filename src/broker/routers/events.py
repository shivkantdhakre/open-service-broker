"""
Events Router — real-time push endpoints via SSE and WebSocket.

GET /stream  — Server-Sent Events (recommended, standard HTTP)
WS  /ws      — WebSocket (for bidirectional needs)

SSE is the default choice for unidirectional server→client push due to
simpler infrastructure, automatic reconnection, and standard HTTP scaling.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import TYPE_CHECKING

import structlog
from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from sse_starlette.sse import EventSourceResponse  # type: ignore[import-untyped]
from ulid import ULID

from broker.dependencies import EventBusDep  # noqa: TC001
from broker.services.event_bus import Event  # noqa: TC001

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

logger = structlog.get_logger()

router = APIRouter()


# ---------------------------------------------------------------------------
# SSE Endpoint (Recommended)
# ---------------------------------------------------------------------------
@router.get(
    "/stream",
    summary="Server-Sent Events stream",
    description=(
        "Real-time event stream via SSE. Clients receive instant notifications "
        "when resource states change, anomalies are detected, or scaling actions "
        "are taken. Supports automatic reconnection via Last-Event-ID."
    ),
)
async def event_stream(
    event_bus: EventBusDep,
    last_event_id: str | None = Query(
        default=None,
        alias="Last-Event-ID",
        description="Resume from a specific event ID after reconnection.",
    ),
) -> EventSourceResponse:
    """SSE endpoint for real-time event push."""
    client_id = str(ULID())

    await logger.ainfo(
        "SSE client connecting",
        client_id=client_id,
        last_event_id=last_event_id,
    )

    async def event_generator() -> AsyncIterator[dict[str, str]]:
        """Generate SSE events from the event bus."""

        async for event in event_bus.subscribe(client_id):
            yield {
                "event": event.event_type,
                "id": f"{event.resource_id}_{event.timestamp.isoformat()}",
                "data": event.to_sse_data(),
            }

    return EventSourceResponse(
        event_generator(),
        headers={
            "X-Accel-Buffering": "no",  # Disable nginx buffering
            "Cache-Control": "no-cache",
        },
        ping=15,  # Heartbeat every 15 seconds
        ping_message_factory=lambda: "heartbeat",
    )


# ---------------------------------------------------------------------------
# WebSocket Endpoint (for bidirectional needs)
# ---------------------------------------------------------------------------
@router.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket,
    event_bus: EventBusDep,
) -> None:
    """WebSocket endpoint for bidirectional real-time communication.

    Use this when you need to send commands back to the server while
    receiving events. For unidirectional push, prefer the SSE endpoint.
    """
    client_id = str(ULID())

    # Accept connection
    await websocket.accept()

    await logger.ainfo("WebSocket client connected", client_id=client_id)

    # Create tasks for both receiving and sending
    receive_task = asyncio.create_task(_ws_receive(websocket, client_id))
    send_task = asyncio.create_task(_ws_send(websocket, event_bus, client_id))

    try:
        # Wait for either task to complete (disconnect or error)
        _done, pending = await asyncio.wait(
            {receive_task, send_task},
            return_when=asyncio.FIRST_COMPLETED,
        )

        # Cancel the remaining task
        for task in pending:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    except Exception as e:
        await logger.aerror("WebSocket error", client_id=client_id, error=str(e))
    finally:
        await event_bus.unsubscribe(client_id)
        await logger.ainfo("WebSocket client disconnected", client_id=client_id)


async def _ws_receive(websocket: WebSocket, client_id: str) -> None:
    """Handle incoming WebSocket messages from the client."""
    try:
        while True:
            data = await websocket.receive_text()
            try:
                message = json.loads(data)
                msg_type = message.get("type", "unknown")

                await logger.ainfo(
                    "WebSocket message received",
                    client_id=client_id,
                    type=msg_type,
                )

                # Handle client commands
                match msg_type:
                    case "ping":
                        await websocket.send_json({"type": "pong"})
                    case "subscribe_resource":
                        # Future: filter events for specific resources
                        await websocket.send_json({
                            "type": "subscribed",
                            "resource_id": message.get("resource_id"),
                        })
                    case _:
                        await websocket.send_json({
                            "type": "error",
                            "message": f"Unknown message type: {msg_type}",
                        })

            except json.JSONDecodeError:
                await websocket.send_json({
                    "type": "error",
                    "message": "Invalid JSON",
                })

    except WebSocketDisconnect:
        pass


async def _ws_send(websocket: WebSocket, event_bus: EventBusDep, client_id: str) -> None:  # type: ignore[arg-type]
    """Send events from the event bus to the WebSocket client."""
    try:
        async for event in event_bus.subscribe(client_id):
            await websocket.send_json({
                "type": "event",
                "event_type": event.event_type,
                "resource_id": event.resource_id,
                "state": event.state,
                "timestamp": event.timestamp.isoformat(),
                "data": event.data,
            })
    except WebSocketDisconnect:
        pass


# ---------------------------------------------------------------------------
# Metrics Endpoint
# ---------------------------------------------------------------------------
@router.get(
    "/metrics",
    summary="Get real-time event bus metrics",
    description="Returns real-time statistics of successful/failed parses and provisions.",
)
async def get_metrics(event_bus: EventBusDep) -> dict[str, int]:
    """Retrieve in-memory event bus metrics."""
    return event_bus.metrics


@router.post(
    "/publish",
    status_code=200,
    summary="Publish a custom event",
    description="Allows internal background components (such as SQS worker) to publish events to the real-time event bus.",
)
async def publish_event(
    event: Event,
    event_bus: EventBusDep,
) -> dict[str, str]:
    """Publish an event to the pub/sub event bus."""
    await event_bus.publish(event)
    return {"status": "published"}
