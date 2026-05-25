"""
Catalog Router — exposes service discovery JSON endpoint and HTML documentation.
"""

from __future__ import annotations

import json

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from typing import Any

router = APIRouter()

CATALOG_SERVICES: list[dict[str, Any]] = [
    {
        "id": "routing-rule",
        "name": "Routing Rule (RDS)",
        "description": "Configure HTTP request routing, path matching, traffic splitting, retry policies, and timeouts on edge proxies.",
        "actions": ["create_route", "update_route", "delete_route"],
        "schema": {
            "route_name": "string (Required) - Unique route config identifier",
            "prefix": "string (Default: '/') - URL path prefix to match",
            "target_cluster": "string (Optional) - Single target upstream cluster",
            "weighted_clusters": "array of objects (Optional) - Traffic splitting weighted clusters (e.g. Canary)",
            "timeout_ms": "integer (Default: 15000) - Request timeout in milliseconds",
            "retry_on": "string (Optional) - Retry conditions (e.g. '5xx,connect-failure')",
            "max_retries": "integer (Default: 1) - Maximum retry attempts (0 to 10)",
        },
        "example_payload": {
            "action": "create_route",
            "target_service": "payments-api",
            "parameters": {
                "route_name": "payments-route",
                "prefix": "/api/v1/payments",
                "target_cluster": "payments-v1",
                "timeout_ms": 5000,
                "retry_on": "5xx,connect-failure",
                "max_retries": 3
            }
        }
    },
    {
        "id": "upstream-cluster",
        "name": "Upstream Cluster (CDS)",
        "description": "Register services, backend endpoints, circuit breaker thresholds, and active health check configurations.",
        "actions": ["create_cluster", "update_cluster"],
        "schema": {
            "cluster_name": "string (Required) - Unique cluster name",
            "lb_policy": "string (Default: 'ROUND_ROBIN') - LB algorithm (ROUND_ROBIN, LEAST_REQUEST, RANDOM)",
            "connect_timeout_ms": "integer (Default: 5000) - Connection timeout in milliseconds",
            "endpoints": "array of objects - Upstream server hosts, ports, and balancing weights",
            "health_check": "object (Optional) - Active health checking criteria (HTTP/TCP/gRPC)",
            "circuit_breaker": "object (Optional) - Connections, pending requests, and request limits",
        },
        "example_payload": {
            "action": "create_cluster",
            "target_service": "user-service",
            "parameters": {
                "cluster_name": "user-service-cluster",
                "lb_policy": "LEAST_REQUEST",
                "endpoints": [
                    {"address": "10.0.1.5", "port": 8080, "weight": 2},
                    {"address": "10.0.1.6", "port": 8080, "weight": 1}
                ],
                "health_check": {
                    "check_type": "HTTP",
                    "path": "/healthz",
                    "interval_ms": 10000,
                    "timeout_ms": 4000
                }
            }
        }
    },
    {
        "id": "rate-limiting",
        "name": "Rate Limiting Filter",
        "description": "Define request limits per unit of time (e.g. seconds/minutes) bound to specific route rules.",
        "actions": ["update_rate_limit"],
        "schema": {
            "name": "string (Required) - Unique rate limit configuration identifier",
            "target_route": "string (Required) - Route name to bind this rate limit to",
            "requests_per_unit": "integer (Required) - Max requests allowed in the specified time unit",
            "unit": "string (Default: 'minute') - Time unit (second, minute, hour, day)",
            "shadow_mode": "boolean (Default: false) - If true, limits are logged/monitored but not enforced (dry-run)",
        },
        "example_payload": {
            "action": "update_rate_limit",
            "target_service": "orders-api",
            "parameters": {
                "name": "orders-rate-limit",
                "target_route": "orders-route",
                "requests_per_unit": 500,
                "unit": "minute",
                "shadow_mode": False
            }
        }
    }
]


@router.get("/api/v1/catalog", summary="Get service discovery schemas")
async def get_catalog_schemas() -> list[dict[str, Any]]:
    """Return JSON schema definitions of all support provisioned resources."""
    return CATALOG_SERVICES


@router.get("/catalog", response_class=HTMLResponse, summary="View self-service catalog documentation portal")
async def get_catalog_portal() -> str:
    """Return a beautiful responsive HTML catalog documentation page."""

    # Generate service panels dynamically
    service_cards_html = ""
    for s in CATALOG_SERVICES:
        # Construct schemas table
        schema_rows = ""
        for field, desc in s["schema"].items():
            schema_rows += f"""
            <tr>
              <td><code>{field}</code></td>
              <td>{desc}</td>
            </tr>
            """

        pretty_json = json.dumps(s["example_payload"], indent=2)

        service_cards_html += f"""
        <section id="{s["id"]}" class="card">
          <div class="card-header">
            <h2>{s["name"]}</h2>
            <div class="actions">
              {" ".join([f'<span class="badge">{a}</span>' for a in s["actions"]])}
            </div>
          </div>
          <p class="description">{s["description"]}</p>

          <div class="card-body">
            <div class="schema-table-container">
              <h3>Configuration Parameters</h3>
              <table>
                <thead>
                  <tr>
                    <th>Parameter Key</th>
                    <th>Specification & Description</th>
                  </tr>
                </thead>
                <tbody>
                  {schema_rows}
                </tbody>
              </table>
            </div>

            <div class="code-container">
              <h3>Example Payload</h3>
              <pre><code>{pretty_json}</code></pre>
            </div>
          </div>
        </section>
        """

    html_content = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
      <meta charset="UTF-8">
      <meta name="viewport" content="width=device-width, initial-scale=1.0">
      <title>Service Broker Catalog Portal</title>
      <link rel="preconnect" href="https://fonts.googleapis.com">
      <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
      <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
      <style>
        :root {{
          --bg-main: #0f172a;
          --bg-card: #1e293b;
          --bg-sidebar: #0b0f19;
          --accent-primary: #6366f1;
          --accent-secondary: #38bdf8;
          --text-main: #f8fafc;
          --text-muted: #94a3b8;
          --border: #334155;
          --badge-bg: #312e81;
          --badge-text: #e0e7ff;
        }}

        * {{
          box-sizing: border-box;
          margin: 0;
          padding: 0;
        }}

        body {{
          font-family: 'Inter', sans-serif;
          background-color: var(--bg-main);
          color: var(--text-main);
          display: flex;
          min-height: 100vh;
          overflow-x: hidden;
        }}

        /* Sidebar navigation */
        aside {{
          width: 280px;
          background-color: var(--bg-sidebar);
          border-right: 1px solid var(--border);
          position: fixed;
          top: 0;
          left: 0;
          height: 100vh;
          padding: 2rem 1.5rem;
          display: flex;
          flex-direction: column;
          gap: 2rem;
          z-index: 10;
        }}

        .logo {{
          font-size: 1.25rem;
          font-weight: 700;
          background: linear-gradient(135deg, var(--accent-primary), var(--accent-secondary));
          -webkit-background-clip: text;
          -webkit-text-fill-color: transparent;
          letter-spacing: -0.5px;
          margin-bottom: 1rem;
        }}

        aside h3 {{
          font-size: 0.75rem;
          text-transform: uppercase;
          letter-spacing: 1.5px;
          color: var(--text-muted);
          margin-bottom: 0.75rem;
        }}

        aside ul {{
          list-style: none;
          display: flex;
          flex-direction: column;
          gap: 0.5rem;
        }}

        aside a {{
          color: var(--text-muted);
          text-decoration: none;
          font-size: 0.95rem;
          font-weight: 500;
          padding: 0.5rem 0.75rem;
          border-radius: 6px;
          display: block;
          transition: all 0.2s ease;
        }}

        aside a:hover {{
          color: var(--text-main);
          background-color: #1e293b;
        }}

        /* Main Content area */
        main {{
          margin-left: 280px;
          flex-grow: 1;
          padding: 3rem 4rem;
          max-width: 1200px;
        }}

        header {{
          margin-bottom: 4rem;
        }}

        header h1 {{
          font-size: 2.5rem;
          font-weight: 800;
          margin-bottom: 0.75rem;
          letter-spacing: -1px;
        }}

        header p {{
          color: var(--text-muted);
          font-size: 1.125rem;
          font-weight: 400;
          max-width: 700px;
          line-height: 1.6;
        }}

        /* Cards and Sections */
        .card {{
          background-color: var(--bg-card);
          border: 1px solid var(--border);
          border-radius: 12px;
          padding: 2rem;
          margin-bottom: 3rem;
          box-shadow: 0 4px 20px -2px rgba(0, 0, 0, 0.4);
          transition: border-color 0.2s ease;
        }}

        .card:hover {{
          border-color: var(--accent-primary);
        }}

        .card-header {{
          display: flex;
          justify-content: space-between;
          align-items: center;
          margin-bottom: 1rem;
          border-bottom: 1px solid var(--border);
          padding-bottom: 1rem;
        }}

        .card-header h2 {{
          font-size: 1.5rem;
          font-weight: 700;
          letter-spacing: -0.5px;
        }}

        .actions {{
          display: flex;
          gap: 0.5rem;
        }}

        .badge {{
          background-color: var(--badge-bg);
          color: var(--badge-text);
          font-size: 0.75rem;
          font-weight: 600;
          padding: 0.25rem 0.75rem;
          border-radius: 100px;
          text-transform: uppercase;
          letter-spacing: 0.5px;
        }}

        .description {{
          color: var(--text-muted);
          line-height: 1.6;
          margin-bottom: 2rem;
          font-size: 1.05rem;
        }}

        .card-body {{
          display: grid;
          grid-template-columns: 1.2fr 1fr;
          gap: 2rem;
        }}

        @media (max-width: 1024px) {{
          .card-body {{
            grid-template-columns: 1fr;
          }}
        }}

        /* Schema Table Styling */
        table {{
          width: 100%;
          border-collapse: collapse;
          text-align: left;
          font-size: 0.9rem;
        }}

        th, td {{
          padding: 0.75rem 1rem;
          border-bottom: 1px solid var(--border);
        }}

        th {{
          color: var(--text-muted);
          font-weight: 600;
          text-transform: uppercase;
          font-size: 0.75rem;
          letter-spacing: 1px;
        }}

        td code {{
          background-color: #0f172a;
          color: var(--accent-secondary);
          padding: 0.2rem 0.5rem;
          border-radius: 4px;
          font-family: monospace;
          font-weight: 600;
        }}

        /* Code Block Styling */
        .code-container pre {{
          background-color: #0f172a;
          border: 1px solid var(--border);
          border-radius: 8px;
          padding: 1.25rem;
          overflow-x: auto;
          font-family: monospace;
          font-size: 0.85rem;
          line-height: 1.5;
          color: #38bdf8;
        }}
      </style>
    </head>
    <body>
      <aside>
        <div class="logo">OSB Platform Catalog</div>
        <div>
          <h3>Resources</h3>
          <ul>
            <li><a href="#routing-rule">Routing Rules</a></li>
            <li><a href="#upstream-cluster">Upstream Clusters</a></li>
            <li><a href="#rate-limiting">Rate Limiting</a></li>
          </ul>
        </div>
      </aside>

      <main>
        <header>
          <h1>Platform Service Catalog</h1>
          <p>
            Welcome to the self-service infrastructure catalog. Developers can discover
            provisionable cloud proxy abstractions and matching actions supported on the edge proxies.
          </p>
        </header>

        {service_cards_html}
      </main>
    </body>
    </html>
    """
    return html_content
