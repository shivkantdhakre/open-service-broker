"""
Global Exception Handler — converts unhandled exceptions into RFC 7807
Problem Detail responses for consistent error formatting.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError

if TYPE_CHECKING:
    from fastapi import FastAPI, Request

logger = structlog.get_logger()


def register_exception_handlers(app: FastAPI) -> None:
    """Register global exception handlers on the FastAPI application."""

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        """Handle Pydantic validation errors from request parsing."""
        return JSONResponse(
            status_code=422,
            content={
                "type": "validation_error",
                "title": "Request validation failed",
                "status": 422,
                "detail": "One or more fields failed validation.",
                "errors": [
                    {
                        "field": ".".join(str(loc) for loc in err["loc"]),
                        "message": err["msg"],
                        "type": err["type"],
                    }
                    for err in exc.errors()
                ],
            },
        )

    @app.exception_handler(ValidationError)
    async def pydantic_validation_handler(
        request: Request,
        exc: ValidationError,
    ) -> JSONResponse:
        """Handle internal Pydantic validation errors."""
        return JSONResponse(
            status_code=500,
            content={
                "type": "internal_validation_error",
                "title": "Internal data validation failed",
                "status": 500,
                "detail": str(exc),
            },
        )

    @app.exception_handler(ValueError)
    async def value_error_handler(
        request: Request,
        exc: ValueError,
    ) -> JSONResponse:
        """Handle ValueError as 400 Bad Request."""
        return JSONResponse(
            status_code=400,
            content={
                "type": "bad_request",
                "title": "Bad Request",
                "status": 400,
                "detail": str(exc),
            },
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(
        request: Request,
        exc: Exception,
    ) -> JSONResponse:
        """Catch-all handler for unhandled exceptions."""
        await logger.aerror(
            "Unhandled exception",
            path=request.url.path,
            method=request.method,
            error=str(exc),
            exc_info=True,
        )

        return JSONResponse(
            status_code=500,
            content={
                "type": "internal_server_error",
                "title": "Internal Server Error",
                "status": 500,
                "detail": "An unexpected error occurred. Check logs for details.",
            },
        )
