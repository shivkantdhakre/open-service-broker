"""
Mock Sovereign Control Plane — Simple FastAPI mock server for Envoy control plane.

Implements the HTTP API endpoints expected by SovereignClient to manage
routes, clusters, and rate limits.
"""

from __future__ import annotations

import logging
from typing import Any
from fastapi import FastAPI, HTTPException, Request, Response
import uvicorn

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mock_sovereign")

app = FastAPI(
    title="Mock Sovereign Control Plane",
    description="Mock management server for Envoy configuration state",
    version="0.1.0",
)

# In-memory configuration store
routes: dict[str, dict[str, Any]] = {}
clusters: dict[str, dict[str, Any]] = {}
rate_limits: dict[str, dict[str, Any]] = {}


@app.middleware("http")
async def log_requests(request: Request, call_next: Any) -> Response:
    """Log details of incoming HTTP requests for local E2E visibility."""
    logger.info("Incoming request: %s %s", request.method, request.url.path)
    if request.method in ("POST", "PUT", "PATCH"):
        try:
            body = await request.json()
            logger.info("Request Body: %s", body)
        except Exception:
            pass
    response: Response = await call_next(request)
    logger.info("Response Status: %s", response.status_code)
    return response


# -----------------------------------------------------------------------------
# Configuration Read
# -----------------------------------------------------------------------------
@app.get("/api/v1/config")
async def get_config() -> dict[str, Any]:
    """Get the complete active configuration state."""
    return {
        "routes": routes,
        "clusters": clusters,
        "rate_limits": rate_limits,
    }


# -----------------------------------------------------------------------------
# Routes API
# -----------------------------------------------------------------------------
@app.get("/api/v1/routes/{route_name}")
async def get_route(route_name: str) -> dict[str, Any]:
    """Retrieve a specific route configuration."""
    if route_name not in routes:
        logger.warning("Route not found: %s", route_name)
        raise HTTPException(status_code=404, detail=f"Route {route_name} not found")
    return routes[route_name]


@app.put("/api/v1/routes/{route_name}", status_code=200)
async def apply_route(route_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Create or update a route configuration (idempotent)."""
    routes[route_name] = payload
    logger.info("Applied route config: %s -> %s", route_name, payload)
    return {"status": "applied", "route_name": route_name}


@app.delete("/api/v1/routes/{route_name}", status_code=200)
async def remove_route(route_name: str) -> dict[str, Any]:
    """Delete a route configuration."""
    if route_name in routes:
        del routes[route_name]
        logger.info("Removed route config: %s", route_name)
    else:
        logger.info("Route %s already absent, skipping delete", route_name)
    return {"status": "removed", "route_name": route_name}


# -----------------------------------------------------------------------------
# Clusters API
# -----------------------------------------------------------------------------
@app.get("/api/v1/clusters/{cluster_name}")
async def get_cluster(cluster_name: str) -> dict[str, Any]:
    """Retrieve a specific cluster configuration."""
    if cluster_name not in clusters:
        logger.warning("Cluster not found: %s", cluster_name)
        raise HTTPException(status_code=404, detail=f"Cluster {cluster_name} not found")
    return clusters[cluster_name]


@app.put("/api/v1/clusters/{cluster_name}", status_code=200)
async def apply_cluster(cluster_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Create or update an upstream cluster configuration (idempotent)."""
    clusters[cluster_name] = payload
    logger.info("Applied cluster config: %s -> %s", cluster_name, payload)
    return {"status": "applied", "cluster_name": cluster_name}


@app.delete("/api/v1/clusters/{cluster_name}", status_code=200)
async def remove_cluster(cluster_name: str) -> dict[str, Any]:
    """Delete an upstream cluster configuration."""
    if cluster_name in clusters:
        del clusters[cluster_name]
        logger.info("Removed cluster config: %s", cluster_name)
    else:
        logger.info("Cluster %s already absent, skipping delete", cluster_name)
    return {"status": "removed", "cluster_name": cluster_name}


# -----------------------------------------------------------------------------
# Rate Limits API
# -----------------------------------------------------------------------------
@app.get("/api/v1/rate-limits/{name}")
async def get_rate_limit(name: str) -> dict[str, Any]:
    """Retrieve a specific rate limit configuration."""
    if name not in rate_limits:
        logger.warning("Rate limit not found: %s", name)
        raise HTTPException(status_code=404, detail=f"Rate limit {name} not found")
    return rate_limits[name]


@app.put("/api/v1/rate-limits/{name}", status_code=200)
async def apply_rate_limit(name: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Create or update a rate limit rule configuration."""
    rate_limits[name] = payload
    logger.info("Applied rate-limit config: %s -> %s", name, payload)
    return {"status": "applied", "name": name}


def run() -> None:
    """Run the mock Sovereign API server."""
    uvicorn.run(app, host="0.0.0.0", port=8080)


if __name__ == "__main__":
    run()
