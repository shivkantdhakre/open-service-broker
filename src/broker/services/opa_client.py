"""
OPA Client — adapter to query local OPA policies over HTTP.
"""

from __future__ import annotations

import httpx
import structlog
from typing import Any

logger = structlog.get_logger()


class OPAClient:
    """Client for Open Policy Agent (OPA) integration."""

    def __init__(self, opa_url: str) -> None:
        self.opa_url = opa_url.rstrip("/")

    async def evaluate_policy(
        self,
        action: str,
        parameters: dict[str, Any],
        blast_radius: dict[str, Any],
        context: dict[str, Any] | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """Query OPA sidecar for compliance validation.

        Returns:
            Dict containing 'is_valid' and 'errors'.
        """
        ctx = context or {}
        payload = {
            "input": {
                "action": action,
                "parameters": parameters,
                "blast_radius": blast_radius,
                "context": ctx,
                "force": force,
            }
        }

        url = f"{self.opa_url}/v1/data/broker/authz"
        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(url, json=payload, timeout=2.0)
                response.raise_for_status()
                data = response.json()
                result = data.get("result", {})
                is_allowed = result.get("allow", False)
                errors = result.get("errors_list", [])
                if not is_allowed and not errors:
                    errors = ["Denied by policy engine (reason unspecified)"]
                return {
                    "is_valid": is_allowed,
                    "errors": errors,
                }
            except httpx.HTTPError as e:
                # If production environment context is specified, fail closed
                if ctx.get("environment") == "production":
                    await logger.aerror(
                        "OPA sidecar is unreachable in production environment. Failing closed.",
                        error=str(e),
                    )
                    return {
                        "is_valid": False,
                        "errors": [f"Failed to reach Policy Engine in production: {str(e)}"],
                    }

                # Otherwise (local dev/staging), soft-enforce: log warning, allow config
                await logger.awarning(
                    "OPA sidecar is unreachable. Soft-enforcement fallback: allowing config change.",
                    error=str(e),
                )
                return {
                    "is_valid": True,
                    "errors": [],
                    "warnings": [f"Failed to reach Policy Engine: {str(e)}"],
                }
