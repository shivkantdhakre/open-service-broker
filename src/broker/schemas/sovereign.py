"""
Pydantic schemas mirroring Sovereign / Envoy xDS configuration structures.

These are the deterministic schemas that all AI-generated configurations must
conform to before being pushed to the Envoy control plane. They serve as the
hard safety boundary between AI output and production infrastructure.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------
class LoadBalancingPolicy(StrEnum):
    """Envoy-supported load balancing algorithms."""

    ROUND_ROBIN = "ROUND_ROBIN"
    LEAST_REQUEST = "LEAST_REQUEST"
    RING_HASH = "RING_HASH"
    RANDOM = "RANDOM"
    MAGLEV = "MAGLEV"


class HealthCheckType(StrEnum):
    """Supported health check protocols."""

    HTTP = "HTTP"
    TCP = "TCP"
    GRPC = "GRPC"


# ---------------------------------------------------------------------------
# Route Configuration
# ---------------------------------------------------------------------------
class RouteMatch(BaseModel):
    """Route matching criteria."""

    prefix: str = Field(default="/", description="URL path prefix to match.")
    headers: dict[str, str] = Field(
        default_factory=dict,
        description="Header key-value pairs that must be present.",
    )
    query_parameters: dict[str, str] = Field(
        default_factory=dict,
        description="Query parameter matching.",
    )


class WeightedCluster(BaseModel):
    """A cluster with an associated traffic weight for canary/split routing."""

    cluster_name: str
    weight: int = Field(..., ge=0, le=100, description="Traffic weight percentage.")


class RouteConfig(BaseModel):
    """Envoy route configuration for the Route Discovery Service (RDS)."""

    route_name: str = Field(..., description="Unique name for this route.")
    match: RouteMatch = Field(default_factory=RouteMatch)
    target_cluster: str | None = Field(
        default=None,
        description="Single target cluster (mutually exclusive with weighted_clusters).",
    )
    weighted_clusters: list[WeightedCluster] | None = Field(
        default=None,
        description="Multiple clusters with traffic splitting weights.",
    )
    timeout_ms: int = Field(default=15000, ge=100, description="Request timeout in milliseconds.")
    retry_on: str | None = Field(
        default=None,
        description="Retry conditions (e.g., '5xx,connect-failure').",
    )
    max_retries: int = Field(default=1, ge=0, le=10)
    metadata: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Cluster Configuration
# ---------------------------------------------------------------------------
class Endpoint(BaseModel):
    """A single upstream endpoint within a cluster."""

    address: str = Field(..., description="IP address or hostname.")
    port: int = Field(..., ge=1, le=65535)
    health_check_port: int | None = None
    weight: int = Field(default=1, ge=1, le=128)


class HealthCheck(BaseModel):
    """Health check configuration for a cluster."""

    check_type: HealthCheckType = HealthCheckType.HTTP
    path: str = Field(default="/health", description="HTTP health check path.")
    interval_ms: int = Field(default=5000, ge=1000)
    timeout_ms: int = Field(default=3000, ge=500)
    unhealthy_threshold: int = Field(default=3, ge=1)
    healthy_threshold: int = Field(default=2, ge=1)


class CircuitBreaker(BaseModel):
    """Circuit breaker thresholds for a cluster."""

    max_connections: int = Field(default=1024, ge=1)
    max_pending_requests: int = Field(default=1024, ge=1)
    max_requests: int = Field(default=1024, ge=1)
    max_retries: int = Field(default=3, ge=0)


class ClusterConfig(BaseModel):
    """Envoy cluster configuration for the Cluster Discovery Service (CDS)."""

    cluster_name: str = Field(..., description="Unique cluster identifier.")
    lb_policy: LoadBalancingPolicy = LoadBalancingPolicy.ROUND_ROBIN
    endpoints: list[Endpoint] = Field(default_factory=list)
    health_check: HealthCheck | None = None
    circuit_breaker: CircuitBreaker | None = None
    connect_timeout_ms: int = Field(default=5000, ge=100)
    metadata: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Listener Configuration
# ---------------------------------------------------------------------------
class ListenerConfig(BaseModel):
    """Envoy listener configuration for the Listener Discovery Service (LDS)."""

    listener_name: str
    address: str = Field(default="0.0.0.0")
    port: int = Field(default=8080, ge=1, le=65535)
    route_config_name: str = Field(
        ...,
        description="Name of the RDS route config to bind to this listener.",
    )
    tls_enabled: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Rate Limiting
# ---------------------------------------------------------------------------
class RateLimitDescriptor(BaseModel):
    """A single rate limit rule descriptor."""

    key: str = Field(..., description="Descriptor key (e.g., 'remote_address', 'path').")
    value: str | None = None


class RateLimitConfig(BaseModel):
    """Rate limiting configuration for a route or virtual host."""

    name: str
    target_route: str = Field(..., description="Route name this rate limit applies to.")
    requests_per_unit: int = Field(..., ge=1, description="Max requests per time unit.")
    unit: str = Field(
        default="minute",
        description="Time unit: 'second', 'minute', 'hour', 'day'.",
    )
    descriptors: list[RateLimitDescriptor] = Field(default_factory=list)
    shadow_mode: bool = Field(
        default=False,
        description="If true, limits are observed but not enforced (dry run).",
    )
