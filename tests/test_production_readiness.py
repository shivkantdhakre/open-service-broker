"""
Tests for production readiness components — Secrets Manager integration and CloudWatch metrics exporter.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import aioboto3
import pytest

from broker.config import Settings, get_settings, retrieve_secrets_from_manager
from broker.services.cloudwatch_exporter import CloudWatchMetricsExporter
from broker.services.event_bus import EventBus


def test_secrets_manager_retrieval():
    """Test retrieving secrets from AWS Secrets Manager."""
    mock_client = MagicMock()
    mock_client.get_secret_value.return_value = {
        "SecretString": '{"LLM_API_KEY": "secret-llm-key", "API_KEYS": "{\\"sk-prod\\": \\"admin\\"}", "API_PORT": "9000", "OPA_ENABLED": "true"}'
    }

    with patch("boto3.client", return_value=mock_client) as mock_boto:
        secrets = retrieve_secrets_from_manager("my-secret", "us-east-1")
        mock_boto.assert_called_once_with(service_name="secretsmanager", region_name="us-east-1")
        mock_client.get_secret_value.assert_called_once_with(SecretId="my-secret")
        assert secrets["LLM_API_KEY"] == "secret-llm-key"
        assert secrets["API_PORT"] == "9000"


def test_settings_override_in_production():
    """Verify that get_settings overrides settings when production_mode is True."""
    # Temporarily override settings config to simulate production mode
    test_secrets = {
        "LLM_API_KEY": "prod-llm-key",
        "API_KEYS": '{"sk-prod-1": "prod-user"}',
        "APP_PORT": "8888",
        "OPA_ENABLED": "true",
    }

    with patch("broker.config.retrieve_secrets_from_manager", return_value=test_secrets) as mock_retrieve:
        # Clear cache first
        get_settings.cache_clear()

        # Let's patch os.environ to set PRODUCTION_MODE=True
        with patch.dict("os.environ", {"PRODUCTION_MODE": "True", "AWS_SECRET_NAME": "my-prod-secret"}):
            settings = get_settings()

            assert settings.production_mode is True
            assert settings.aws_endpoint_url is None
            assert settings.llm_api_key == "prod-llm-key"
            assert settings.api_keys == {"sk-prod-1": "prod-user"}
            assert settings.app_port == 8888
            assert settings.opa_enabled is True
            mock_retrieve.assert_called_once_with("my-prod-secret", "us-east-1")

        # Clean cache after test
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_cloudwatch_metrics_exporter():
    """Verify that CloudWatchMetricsExporter successfully publishes metrics to CloudWatch."""
    event_bus = EventBus()
    # Populate some event bus metrics
    event_bus.metrics["intent_parse_success"] = 5
    event_bus.metrics["intent_parse_failed"] = 1
    event_bus.metrics["provision_success"] = 10
    event_bus.metrics["provision_failed"] = 2

    # Mock settings and session
    settings = Settings()
    settings.aws_endpoint_url = "http://localhost:4566"
    settings.aws_region = "us-east-1"

    mock_cw_client = AsyncMock()
    mock_cw_client.__aenter__.return_value = mock_cw_client
    mock_cw_client.__aexit__.return_value = None

    mock_session = MagicMock(spec=aioboto3.Session)
    mock_session.client.return_value = mock_cw_client

    exporter = CloudWatchMetricsExporter(
        event_bus=event_bus,
        session=mock_session,
        settings=settings,
        namespace="OSB/TestBroker",
        interval=0.1,
    )

    await exporter.export_metrics()

    mock_session.client.assert_called_once_with(
        "cloudwatch",
        endpoint_url=settings.aws_endpoint_url,
        region_name=settings.aws_region,
    )

    # Check put_metric_data argument details
    mock_cw_client.put_metric_data.assert_called_once()
    kwargs = mock_cw_client.put_metric_data.call_args[1]
    assert kwargs["Namespace"] == "OSB/TestBroker"
    
    metric_data = kwargs["MetricData"]
    # Verify the metric values are correctly parsed to float
    metrics_dict = {m["MetricName"]: m["Value"] for m in metric_data}
    assert metrics_dict["intent_parse_success"] == 5.0
    assert metrics_dict["intent_parse_failed"] == 1.0
    assert metrics_dict["provision_success"] == 10.0
    assert metrics_dict["provision_failed"] == 2.0
