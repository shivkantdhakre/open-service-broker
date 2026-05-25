"""
Structured Logging Middleware — logs every HTTP request/response with
timing, status, and path information in structured JSON format.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import structlog
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response

logger = structlog.get_logger()


class StructuredLoggingMiddleware(BaseHTTPMiddleware):
    """Middleware that logs request/response metadata in structured format."""

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        start_time = time.perf_counter()

        # Skip logging for health checks to reduce noise
        if request.url.path in ("/health", "/health/ready"):
            return await call_next(request)

        response = await call_next(request)

        duration_ms = (time.perf_counter() - start_time) * 1000

        await logger.ainfo(
            "HTTP request",
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            duration_ms=round(duration_ms, 2),
            client_ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent", ""),
        )

        return response
