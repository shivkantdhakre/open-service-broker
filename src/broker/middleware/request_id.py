"""
Request ID Middleware — injects a unique X-Request-ID header into every
request and response for distributed tracing and log correlation.
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from ulid import ULID

REQUEST_ID_HEADER = "X-Request-ID"


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Middleware that assigns a unique request ID to every request."""

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        # Use existing header if provided (from upstream proxy), otherwise generate
        request_id = request.headers.get(REQUEST_ID_HEADER, str(ULID()))

        # Store in request state for access in route handlers
        request.state.request_id = request_id

        # Set in structlog context for automatic log correlation
        import structlog

        structlog.contextvars.bind_contextvars(request_id=request_id)

        response = await call_next(request)

        # Echo the request ID back to the client
        response.headers[REQUEST_ID_HEADER] = request_id

        # Clear the context for this request
        structlog.contextvars.unbind_contextvars("request_id")

        return response
