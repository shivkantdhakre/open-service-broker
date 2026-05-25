"""
Centralized application configuration via environment variables.

Uses pydantic-settings to parse and validate environment variables with
sensible defaults targeting LocalStack for local development.
"""

from __future__ import annotations

import contextlib
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Resolve .env from project root (two levels up from this file)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_ENV_FILE = _PROJECT_ROOT / ".env"


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # -------------------------------------------------------------------------
    # AWS
    # -------------------------------------------------------------------------
    aws_endpoint_url: str | None = "http://localhost:4566"
    aws_region: str = "us-east-1"
    aws_access_key_id: str = "test"
    aws_secret_access_key: str = "test"

    # DynamoDB
    dynamodb_table_name: str = "broker-resources"
    dynamodb_metrics_table_name: str = "broker-metrics"

    # SQS
    sqs_queue_url: str = "http://localhost:4566/000000000000/broker-tasks"
    sqs_dlq_url: str = "http://localhost:4566/000000000000/broker-tasks-dlq"

    # -------------------------------------------------------------------------
    # LLM / AI
    # -------------------------------------------------------------------------
    llm_provider: str = "openai"
    llm_api_key: str = ""
    llm_model: str = "gpt-4o"
    llm_max_tokens: int = 4096
    llm_temperature: float = 0.1

    # -------------------------------------------------------------------------
    # Sovereign (Envoy Control Plane)
    # -------------------------------------------------------------------------
    sovereign_api_url: str = "http://localhost:8080"

    # -------------------------------------------------------------------------
    # Application
    # -------------------------------------------------------------------------
    log_level: str = "DEBUG"
    cors_origins: list[str] = ["http://localhost:3000", "http://localhost:5173"]
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    production_mode: bool = False
    environment: str = "production"
    aws_secret_name: str = "open-service-broker-secrets"

    # -------------------------------------------------------------------------
    # API Key Authentication
    # -------------------------------------------------------------------------
    # JSON dict mapping raw keys → user identities.  Empty dict disables auth.
    # Example: '{"sk-dev-abc123": "alice@team.com", "sk-ci-xyz": "ci-bot"}'
    api_keys: dict[str, str] = {}

    # -------------------------------------------------------------------------
    # Predictive Scaling
    # -------------------------------------------------------------------------
    scaling_prediction_horizon_minutes: int = 30
    scaling_confidence_threshold: float = 0.8
    scaling_cooldown_seconds: int = 300

    # -------------------------------------------------------------------------
    # OPA Compliance
    # -------------------------------------------------------------------------
    opa_enabled: bool = False
    opa_url: str = "http://localhost:8181"

    # -------------------------------------------------------------------------
    # Response Caching
    # -------------------------------------------------------------------------
    response_cache_enabled: bool = True
    redis_url: str | None = None

    @field_validator("cors_origins", mode="before")
    @classmethod
    def parse_cors_origins(cls, v: Any) -> list[str]:
        """Accept both JSON array strings and Python lists."""
        if isinstance(v, str):
            return json.loads(v)  # type: ignore[no-any-return]
        return v  # type: ignore[no-any-return]

    @field_validator("api_keys", mode="before")
    @classmethod
    def parse_api_keys(cls, v: Any) -> dict[str, str]:
        """Accept both JSON object strings and Python dicts."""
        if isinstance(v, str):
            if not v.strip():
                return {}
            return json.loads(v)  # type: ignore[no-any-return]
        return v  # type: ignore[no-any-return]


def retrieve_secrets_from_manager(secret_name: str, region_name: str) -> dict[str, Any]:
    """Retrieve secrets from AWS Secrets Manager."""
    import boto3
    try:
        # Create a Secrets Manager client using default credentials chain
        client = boto3.client(
            service_name="secretsmanager",
            region_name=region_name,
        )
        response = client.get_secret_value(SecretId=secret_name)
        if "SecretString" in response:
            return json.loads(response["SecretString"])  # type: ignore[no-any-return]
        elif "SecretBinary" in response:
            import base64
            decoded = base64.b64decode(response["SecretBinary"]).decode("utf-8")
            return json.loads(decoded)  # type: ignore[no-any-return]
    except Exception as e:
        print(f"Error retrieving secret {secret_name} from region {region_name}: {e}")
    return {}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached singleton of application settings."""
    settings = Settings()
    if settings.production_mode:
        # Override endpoint URL to None for real AWS connection
        settings.aws_endpoint_url = None

        # Retrieve secrets from AWS Secrets Manager
        secrets = retrieve_secrets_from_manager(settings.aws_secret_name, settings.aws_region)
        for key, value in secrets.items():
            key_lower = key.lower()
            if hasattr(settings, key_lower):
                # Handle dictionary / list field parsing if stringified in secret
                if key_lower in ("api_keys", "cors_origins") and isinstance(value, str):
                    with contextlib.suppress(Exception):
                        value = json.loads(value)

                # Check annotations and handle type casting if necessary
                field_type = settings.model_fields[key_lower].annotation
                if field_type is int:
                    with contextlib.suppress(Exception):
                        value = int(value)
                elif field_type is float:
                    with contextlib.suppress(Exception):
                        value = float(value)
                elif field_type is bool and isinstance(value, str):
                    value = value.lower() in ("true", "1", "yes")

                setattr(settings, key_lower, value)
    return settings
