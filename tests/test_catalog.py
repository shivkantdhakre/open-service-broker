"""
Unit tests for the Self-Service Catalog Router.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from broker.main import app


@pytest.fixture
def client():
    """FastAPI TestClient fixture."""
    return TestClient(app)


def test_get_catalog_json_endpoint(client):
    """GET /api/v1/catalog should return the JSON array of catalog schemas."""
    response = client.get("/api/v1/catalog")
    assert response.status_code == 200
    
    data = response.json()
    assert isinstance(data, list)
    assert len(data) > 0
    
    # Verify the structure of the first catalog item
    item = data[0]
    assert "id" in item
    assert "name" in item
    assert "description" in item
    assert "actions" in item
    assert "schema" in item
    assert "example_payload" in item


def test_get_catalog_html_portal(client):
    """GET /catalog should return a beautiful HTML portal page."""
    response = client.get("/catalog")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    
    html = response.text
    assert "<!DOCTYPE html>" in html
    assert "OSB Platform Catalog" in html
    assert "Platform Service Catalog" in html
    assert "Routing Rule" in html
