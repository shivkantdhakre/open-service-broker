"""
Sovereign Client — async HTTP client for the Envoy control plane.

All interactions with the Sovereign management server are routed through
this client. Operations are idempotent for safe retries.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx
import structlog

if TYPE_CHECKING:
    from broker.config import Settings
    from broker.schemas.sovereign import ClusterConfig, RateLimitConfig, RouteConfig

logger = structlog.get_logger()


class SovereignError(Exception):
    """Raised when a Sovereign API call fails."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class SovereignClient:
    """Async HTTP client for the Sovereign Envoy management server."""

    def __init__(self, settings: Settings) -> None:
        self._base_url = settings.sovereign_api_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(30.0, connect=10.0),
            headers={"Content-Type": "application/json"},
        )

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    # -------------------------------------------------------------------------
    # Route operations
    # -------------------------------------------------------------------------
    async def apply_route(self, config: RouteConfig) -> None:
        """Apply a route configuration to Sovereign.

        This is idempotent — applying the same route twice has no effect.
        """
        await self._request(
            "PUT",
            f"/api/v1/routes/{config.route_name}",
            json_data=config.model_dump(mode="json"),
        )
        await logger.ainfo("Route applied to Sovereign", route_name=config.route_name)

    async def remove_route(self, route_name: str) -> None:
        """Remove a route from Sovereign."""
        await self._request("DELETE", f"/api/v1/routes/{route_name}")
        await logger.ainfo("Route removed from Sovereign", route_name=route_name)

    # -------------------------------------------------------------------------
    # Cluster operations
    # -------------------------------------------------------------------------
    async def apply_cluster(self, config: ClusterConfig) -> None:
        """Apply a cluster configuration to Sovereign."""
        await self._request(
            "PUT",
            f"/api/v1/clusters/{config.cluster_name}",
            json_data=config.model_dump(mode="json"),
        )
        await logger.ainfo("Cluster applied to Sovereign", cluster_name=config.cluster_name)

    async def remove_cluster(self, cluster_name: str) -> None:
        """Remove a cluster from Sovereign."""
        await self._request("DELETE", f"/api/v1/clusters/{cluster_name}")
        await logger.ainfo("Cluster removed from Sovereign", cluster_name=cluster_name)

    # -------------------------------------------------------------------------
    # Rate limit operations
    # -------------------------------------------------------------------------
    async def apply_rate_limit(self, config: RateLimitConfig) -> None:
        """Apply a rate limit configuration."""
        await self._request(
            "PUT",
            f"/api/v1/rate-limits/{config.name}",
            json_data=config.model_dump(mode="json"),
        )
        await logger.ainfo("Rate limit applied to Sovereign", name=config.name)

    # -------------------------------------------------------------------------
    # Read operations
    # -------------------------------------------------------------------------
    async def get_current_config(self) -> dict[str, Any]:
        """Get the complete current configuration from Sovereign."""
        response = await self._request("GET", "/api/v1/config")
        return response or {}

    async def get_route(self, route_name: str) -> dict[str, Any] | None:
        """Get a specific route configuration."""
        try:
            response = await self._request("GET", f"/api/v1/routes/{route_name}")
            return response
        except SovereignError as e:
            if e.status_code == 404:
                return None
            raise

    async def get_cluster(self, cluster_name: str) -> dict[str, Any] | None:
        """Get a specific cluster configuration."""
        try:
            response = await self._request("GET", f"/api/v1/clusters/{cluster_name}")
            return response
        except SovereignError as e:
            if e.status_code == 404:
                return None
            raise

    async def get_rate_limit(self, name: str) -> dict[str, Any] | None:
        """Get a specific rate limit configuration."""
        try:
            response = await self._request("GET", f"/api/v1/rate-limits/{name}")
            return response
        except SovereignError as e:
            if e.status_code == 404:
                return None
            raise

    # -------------------------------------------------------------------------
    # Internal HTTP helper
    # -------------------------------------------------------------------------
    async def _request(
        self,
        method: str,
        path: str,
        json_data: dict[str, Any] | None = None,
        max_retries: int = 3,
    ) -> dict[str, Any] | None:
        """Execute an HTTP request with retry logic.

        Args:
            method: HTTP method.
            path: URL path (appended to base URL).
            json_data: Optional JSON body.
            max_retries: Number of retry attempts.

        Returns:
            Parsed JSON response, or None for 204 responses.

        Raises:
            SovereignError: If all retries are exhausted.
        """
        last_error: Exception | None = None

        for attempt in range(max_retries):
            try:
                response = await self._client.request(
                    method=method,
                    url=path,
                    json=json_data,
                )

                if response.status_code == 204:
                    return None

                if response.status_code >= 400:
                    raise SovereignError(
                        f"Sovereign API error: {response.status_code} — {response.text}",
                        status_code=response.status_code,
                    )

                return response.json()  # type: ignore[no-any-return]

            except httpx.HTTPError as e:
                last_error = e
                await logger.awarning(
                    "Sovereign API request failed, retrying",
                    method=method,
                    path=path,
                    attempt=attempt + 1,
                    error=str(e),
                )

        raise SovereignError(
            f"Sovereign API request failed after {max_retries} attempts: {last_error}"
        )
