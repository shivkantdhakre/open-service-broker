"""
API Key Authentication Middleware.

Validates the ``X-API-Key`` header against a configured allow-list and
injects the authenticated user identity into ``request.state.authenticated_user``
so downstream handlers can record an audit trail.

Endpoints that are exempt from authentication (health, docs) are listed in
``_PUBLIC_PREFIXES``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse

if TYPE_CHECKING:
    from fastapi import Request, Response

logger = structlog.get_logger()

# Paths that never require an API key
_PUBLIC_PREFIXES: tuple[str, ...] = (
    "/health",
    "/docs",
    "/redoc",
    "/openapi.json",
)


class APIKeyMiddleware(BaseHTTPMiddleware):
    """Lightweight API-key gate.

    When ``api_keys`` is empty the middleware is effectively a no-op so that
    local development remains frictionless.  In production the env var
    ``API_KEYS`` provides the allow-list.
    """

    def __init__(self, app, api_keys: dict[str, str] | None = None) -> None:  # type: ignore[no-untyped-def]
        super().__init__(app)
        # api_keys maps raw key → human-readable identity (e.g. email)
        self._api_keys: dict[str, str] = api_keys or {}

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        # Skip auth for public endpoints
        if any(request.url.path.startswith(p) for p in _PUBLIC_PREFIXES):
            request.state.authenticated_user = "anonymous"
            return await call_next(request)

        # If no keys are configured, auth is disabled (dev mode)
        if not self._api_keys:
            request.state.authenticated_user = "dev-user"
            return await call_next(request)

        api_key = request.headers.get("X-API-Key")

        if not api_key:
            return JSONResponse(
                status_code=401,
                content={
                    "type": "authentication_error",
                    "title": "Missing API Key",
                    "detail": "Provide a valid API key via the X-API-Key header.",
                },
            )

        identity = self._api_keys.get(api_key)
        if identity is None:
            await logger.awarning(
                "Invalid API key presented",
                key_prefix=api_key[:8] + "…" if len(api_key) > 8 else "***",
            )
            return JSONResponse(
                status_code=403,
                content={
                    "type": "authentication_error",
                    "title": "Invalid API Key",
                    "detail": "The provided API key is not recognised.",
                },
            )

        request.state.authenticated_user = identity
        await logger.adebug("Authenticated request", user=identity)
        return await call_next(request)
