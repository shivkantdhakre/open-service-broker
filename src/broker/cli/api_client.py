"""
Async HTTP client for the Advanced AI Service Broker API.

Wraps ``httpx.AsyncClient`` with API-key authentication, structured error
handling, and SSE streaming support for the real-time event endpoint.
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Coroutine

import httpx

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from broker.cli.config import CLIConfig


# ---------------------------------------------------------------------------
# Error types
# ---------------------------------------------------------------------------
class APIError(Exception):
    """Raised when the broker API returns a non-2xx response."""

    def __init__(
        self,
        status_code: int,
        detail: str,
        body: dict[str, Any] | None = None,
    ) -> None:
        self.status_code = status_code
        self.detail = detail
        self.body = body
        super().__init__(f"HTTP {status_code}: {detail}")


class ConnectionError(Exception):  # noqa: A001
    """Raised when the broker API is unreachable."""


# ---------------------------------------------------------------------------
# SSE event model
# ---------------------------------------------------------------------------
@dataclass
class SSEEvent:
    """A single Server-Sent Event parsed from the stream."""

    event: str = "message"
    id: str = ""
    data: str = ""

    @property
    def json_data(self) -> dict[str, Any]:
        """Parse the ``data`` field as JSON."""
        try:
            result: dict[str, Any] = json.loads(self.data)
            return result
        except (json.JSONDecodeError, TypeError):
            return {"raw": self.data}


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------
class BrokerAPIClient:
    """High-level async client for the Open Service Broker API.

    Usage::

        cfg = CLIConfig(api_url="http://localhost:8000", api_key="sk-...")
        async with BrokerAPIClient(cfg) as client:
            resp = await client.parse_intent("give me a load balancer")
    """

    def __init__(self, config: CLIConfig) -> None:
        self._config = config
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> BrokerAPIClient:
        self._client = httpx.AsyncClient(
            base_url=self._config.base_url,
            headers=self._config.headers,
            timeout=httpx.Timeout(self._config.timeout, connect=10.0),
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._client:
            await self._client.aclose()

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("BrokerAPIClient must be used as an async context manager")
        return self._client

    # -- helpers ------------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute an HTTP request and return the parsed JSON body."""
        try:
            resp = await self.client.request(
                method,
                path,
                json=json_body,
                params=params,
            )
        except httpx.ConnectError as e:
            raise ConnectionError(
                f"Cannot reach the broker API at {self._config.base_url}. "
                "Is the server running?\n"
                f"  ↳ {e}"
            ) from e
        except httpx.TimeoutException as e:
            raise ConnectionError(
                f"Request to {self._config.base_url}{path} timed out "
                f"after {self._config.timeout}s."
            ) from e

        if resp.status_code >= 400:
            try:
                body = resp.json()
            except Exception:
                body = None
            detail = ""
            if body:
                detail = body.get("detail", body.get("title", str(body)))
                if isinstance(detail, dict):
                    detail = detail.get("detail", str(detail))
            raise APIError(resp.status_code, detail or resp.text, body)

        result: dict[str, Any] = resp.json()
        return result

    async def _request_list(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Execute an HTTP request that returns a JSON array."""
        try:
            resp = await self.client.request(method, path, params=params)
        except httpx.ConnectError as e:
            raise ConnectionError(
                f"Cannot reach the broker API at {self._config.base_url}."
            ) from e
        except httpx.TimeoutException as e:
            raise ConnectionError(
                f"Request timed out after {self._config.timeout}s."
            ) from e

        if resp.status_code >= 400:
            try:
                body = resp.json()
            except Exception:
                body = None
            detail = body.get("detail", resp.text) if body else resp.text
            raise APIError(resp.status_code, str(detail), body)

        result: list[dict[str, Any]] = resp.json()
        return result

    # -- Intent endpoints ---------------------------------------------------

    async def parse_intent(
        self,
        natural_language: str,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """POST /api/v1/intent/parse"""
        payload: dict[str, Any] = {"natural_language": natural_language}
        if context:
            payload["context"] = context
        return await self._request("POST", "/api/v1/intent/parse", json_body=payload)

    async def apply_intent(
        self,
        request_id: str,
        parsed_configuration: dict[str, Any],
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        """POST /api/v1/intent/apply"""
        return await self._request(
            "POST",
            "/api/v1/intent/apply",
            json_body={
                "request_id": request_id,
                "parsed_configuration": parsed_configuration,
                "force": force,
            },
        )

    async def get_intent_history(self, limit: int = 50) -> list[dict[str, Any]]:
        """GET /api/v1/intent/history"""
        return await self._request_list(
            "GET", "/api/v1/intent/history", params={"limit": limit}
        )

    # -- Resource endpoints -------------------------------------------------

    async def list_resources(
        self,
        resource_type: str | None = None,
        state: str | None = None,
    ) -> list[dict[str, Any]]:
        """GET /api/v1/resources"""
        params: dict[str, Any] = {}
        if resource_type:
            params["resource_type"] = resource_type
        if state:
            params["state"] = state
        return await self._request_list("GET", "/api/v1/resources", params=params)

    async def get_resource(self, resource_id: str) -> dict[str, Any]:
        """GET /api/v1/resources/{id}"""
        return await self._request("GET", f"/api/v1/resources/{resource_id}")

    async def delete_resource(self, resource_id: str) -> dict[str, Any]:
        """DELETE /api/v1/resources/{id}"""
        return await self._request("DELETE", f"/api/v1/resources/{resource_id}")

    # -- Scaling endpoints --------------------------------------------------

    async def get_predictions(self) -> list[dict[str, Any]]:
        """GET /api/v1/scaling/predictions"""
        return await self._request_list("GET", "/api/v1/scaling/predictions")

    async def get_anomalies(self) -> list[dict[str, Any]]:
        """GET /api/v1/scaling/anomalies"""
        return await self._request_list("GET", "/api/v1/scaling/anomalies")

    # -- Health endpoints ---------------------------------------------------

    async def health(self) -> dict[str, Any]:
        """GET /health"""
        return await self._request("GET", "/health")

    async def health_ready(self) -> dict[str, Any]:
        """GET /health/ready"""
        return await self._request("GET", "/health/ready")

    # -- Maintenance endpoints -----------------------------------------------

    async def analyze_codebase(
        self,
        repository_path: str = ".",
        include_patterns: list[str] | None = None,
        exclude_patterns: list[str] | None = None,
    ) -> dict[str, Any]:
        """POST /api/v1/maintenance/analyze"""
        payload: dict[str, Any] = {"repository_path": repository_path}
        if include_patterns is not None:
            payload["include_patterns"] = include_patterns
        if exclude_patterns is not None:
            payload["exclude_patterns"] = exclude_patterns
        return await self._request("POST", "/api/v1/maintenance/analyze", json_body=payload)

    async def get_drift_alerts(self) -> list[dict[str, Any]]:
        """GET /api/v1/maintenance/drift"""
        return await self._request_list("GET", "/api/v1/maintenance/drift")

    async def list_proposals(self) -> list[dict[str, Any]]:
        """GET /api/v1/maintenance/proposals"""
        return await self._request_list("GET", "/api/v1/maintenance/proposals")

    async def approve_proposal(self, proposal_id: str) -> dict[str, Any]:
        """POST /api/v1/maintenance/proposals/{proposal_id}/approve"""
        return await self._request("POST", f"/api/v1/maintenance/proposals/{proposal_id}/approve")

    async def get_metrics(self) -> dict[str, Any]:
        """GET /api/v1/events/metrics"""
        return await self._request("GET", "/api/v1/events/metrics")

    # -- SSE streaming ------------------------------------------------------

    async def stream_events(self) -> AsyncIterator[SSEEvent]:
        """Connect to the SSE event stream and yield parsed events.

        This is a long-lived connection — use ``async for event in ...``
        and break when you want to disconnect.
        """
        try:
            async with self.client.stream(
                "GET",
                "/api/v1/events/stream",
                timeout=httpx.Timeout(None, connect=10.0),
            ) as response:
                if response.status_code >= 400:
                    raise APIError(
                        response.status_code,
                        "Failed to connect to event stream",
                    )

                event = SSEEvent()
                async for line in response.aiter_lines():
                    line = line.strip()

                    if not line:
                        # Empty line = event boundary
                        if event.data:
                            yield event
                        event = SSEEvent()
                        continue

                    if line.startswith("event:"):
                        event.event = line[6:].strip()
                    elif line.startswith("id:"):
                        event.id = line[3:].strip()
                    elif line.startswith("data:"):
                        data_part = line[5:].strip()
                        if event.data:
                            event.data += "\n" + data_part
                        else:
                            event.data = data_part
                    elif line.startswith(":"):
                        # SSE comment / heartbeat — skip silently
                        pass

        except httpx.HTTPError as e:
            raise ConnectionError(
                f"Disconnected or cannot connect to event stream at {self._config.base_url}.\n"
                f"  ↳ {e}"
            ) from e


def run_async(coro: Coroutine[Any, Any, Any]) -> Any:
    """Run an async coroutine in a new event loop.

    Works around Windows-specific issues with ``asyncio.run`` by forcing
    the selector event loop policy.
    """
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    return asyncio.run(coro)
