"""
CLI configuration — environment-based settings for the ``osb`` client.

Reads from environment variables with sensible local-development defaults.
Values can be overridden per-invocation via CLI flags.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class CLIConfig:
    """Immutable configuration for a single CLI invocation."""

    api_url: str = field(
        default_factory=lambda: os.environ.get(
            "OSB_API_URL", "http://localhost:8000"
        ),
    )
    api_key: str = field(
        default_factory=lambda: os.environ.get("OSB_API_KEY", ""),
    )
    output_format: str = field(default="rich")  # "rich" | "json"
    verbose: bool = field(default=False)
    timeout: float = field(default=30.0)

    @property
    def base_url(self) -> str:
        """Normalised base URL (strip trailing slash)."""
        return self.api_url.rstrip("/")

    @property
    def headers(self) -> dict[str, str]:
        """Common HTTP headers including the API key when set."""
        h: dict[str, str] = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self.api_key:
            h["X-API-Key"] = self.api_key
        return h


def build_config(
    *,
    api_url: str | None = None,
    api_key: str | None = None,
    output_format: str = "rich",
    verbose: bool = False,
    timeout: float = 30.0,
) -> CLIConfig:
    """Build a ``CLIConfig`` merging explicit overrides with env defaults."""
    return CLIConfig(
        api_url=api_url or os.environ.get("OSB_API_URL", "http://localhost:8000"),
        api_key=api_key or os.environ.get("OSB_API_KEY", ""),
        output_format=output_format,
        verbose=verbose,
        timeout=timeout,
    )
