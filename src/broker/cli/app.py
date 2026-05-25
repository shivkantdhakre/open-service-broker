"""
``osb`` — main Typer application.

Command tree::

    osb intent parse  "…"       Parse natural language → config
    osb intent apply  <id>      Apply a validated configuration
    osb intent history          Audit trail of past translations

    osb resources list          List managed resources
    osb resources show <id>     Resource detail
    osb resources delete <id>   Decommission a resource

    osb events watch            Stream real-time SSE events

    osb health                  Liveness / readiness probe

Global flags::

    --api-url   Override broker API base URL  (env: OSB_API_URL)
    --api-key   Override API key              (env: OSB_API_KEY)
    --json      Output raw JSON instead of rich panels
    --verbose   Show debug-level request info
"""

from __future__ import annotations

import typer

from broker.cli.api_client import APIError, BrokerAPIClient, run_async
from broker.cli.api_client import ConnectionError as BrokerConnectionError
from broker.cli.config import CLIConfig, build_config
from broker.cli.display import (
    console,
    display_apply_response,
    display_banner,
    display_connection_error,
    display_error,
    display_health,
    display_intent_history,
    display_intent_response,
    display_json,
    display_resource_detail,
    display_resources_table,
    display_sse_event,
    display_sse_heartbeat,
    display_coupling_report,
    display_drift_alerts,
    display_proposals_table,
    display_approved_proposal,
    display_metrics,
)

# ---------------------------------------------------------------------------
# Root app
# ---------------------------------------------------------------------------
app = typer.Typer(
    name="osb",
    help="Open Service Broker CLI — AI-driven infrastructure provisioning.",
    no_args_is_help=True,
    rich_markup_mode="rich",
    pretty_exceptions_enable=True,
)

# ---------------------------------------------------------------------------
# Sub-command groups
# ---------------------------------------------------------------------------
intent_app = typer.Typer(
    name="intent",
    help="🧠 AI intent parsing — translate natural language to infrastructure configs.",
    no_args_is_help=True,
)
app.add_typer(intent_app, name="intent")

resources_app = typer.Typer(
    name="resources",
    help="📦 Managed resources — list, inspect, and delete provisioned infrastructure.",
    no_args_is_help=True,
)
app.add_typer(resources_app, name="resources")

events_app = typer.Typer(
    name="events",
    help="📡 Real-time events — stream infrastructure state changes.",
    no_args_is_help=True,
)
app.add_typer(events_app, name="events")

maintenance_app = typer.Typer(
    name="maintenance",
    help="🛠️ Codebase maintenance — trigger analyses, detect drift, and approve refactoring proposals.",
    no_args_is_help=True,
)
app.add_typer(maintenance_app, name="maintenance")

# ---------------------------------------------------------------------------
# Shared state (populated by callback)
# ---------------------------------------------------------------------------
_cli_config: CLIConfig | None = None


def _get_config() -> CLIConfig:
    """Return the current CLI config or build a default."""
    if _cli_config is not None:
        return _cli_config
    return build_config()


# ---------------------------------------------------------------------------
# Root callback — global flags
# ---------------------------------------------------------------------------
@app.callback()
def root_callback(
    api_url: str | None = typer.Option(
        None,
        "--api-url",
        envvar="OSB_API_URL",
        help="Broker API base URL.",
    ),
    api_key: str | None = typer.Option(
        None,
        "--api-key",
        envvar="OSB_API_KEY",
        help="API key for authentication.",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Output raw JSON instead of rich panels.",
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Show detailed request/response info.",
    ),
) -> None:
    """Open Service Broker CLI — AI-driven infrastructure from natural language."""
    global _cli_config
    _cli_config = build_config(
        api_url=api_url,
        api_key=api_key,
        output_format="json" if json_output else "rich",
        verbose=verbose,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Intent commands
# ═══════════════════════════════════════════════════════════════════════════

@intent_app.command("parse")
def intent_parse(
    text: str = typer.Argument(
        ...,
        help="Natural language description of the desired infrastructure change.",
    ),
    context_env: str | None = typer.Option(
        None,
        "--env",
        help="Target environment context (e.g. staging, production).",
    ),
    context_ns: str | None = typer.Option(
        None,
        "--namespace",
        "-n",
        help="Target namespace context.",
    ),
) -> None:
    """Parse a natural language request into an infrastructure configuration.

    Examples:

        osb intent parse "Route 30% of traffic to canary"

        osb intent parse "Rate limit payments to 500 req/min" --env staging
    """
    cfg = _get_config()
    context: dict[str, str] | None = None
    if context_env or context_ns:
        context = {}
        if context_env:
            context["environment"] = context_env
        if context_ns:
            context["namespace"] = context_ns

    async def _run() -> None:
        async with BrokerAPIClient(cfg) as client:
            result = await client.parse_intent(text, context)
            if cfg.output_format == "json":
                display_json(result)
            else:
                display_intent_response(result)

    _execute(_run)


@intent_app.command("apply")
def intent_apply(
    request_id: str = typer.Argument(
        ...,
        help="Request ID from a prior 'osb intent parse' response.",
    ),
    config_json: str = typer.Option(
        ...,
        "--config",
        "-c",
        help="Parsed configuration as a JSON string (from the parse response).",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Bypass blast radius warnings (requires elevated privileges).",
    ),
    watch: bool = typer.Option(
        False,
        "--watch",
        "-w",
        help="After applying, stream SSE events until the resource reaches a terminal state.",
    ),
) -> None:
    """Apply a previously parsed and validated configuration.

    Example:

        osb intent apply 01J6F... -c '{"action":"create_route", ...}'
    """
    import json as json_mod

    cfg = _get_config()

    try:
        parsed_config = json_mod.loads(config_json)
    except json_mod.JSONDecodeError as e:
        display_error("Invalid JSON in --config", str(e))
        raise typer.Exit(1) from e

    async def _run() -> None:
        async with BrokerAPIClient(cfg) as client:
            result = await client.apply_intent(request_id, parsed_config, force=force)
            if cfg.output_format == "json":
                display_json(result)
            else:
                display_apply_response(result)

            if watch:
                resource_id = result.get("resource_id", "")
                console.print(
                    f"\n[osb.muted]Watching events for resource "
                    f"[osb.value]{resource_id}[/osb.value]…  "
                    f"Press Ctrl+C to stop.[/]"
                )
                try:
                    async for event in client.stream_events():
                        data = event.json_data
                        display_sse_event(data, event.event)
                        # Stop when this resource reaches a terminal state
                        if (
                            data.get("resource_id") == resource_id
                            and data.get("state", "").upper() in {"ACTIVE", "FAILED", "DELETED"}
                        ):
                            break
                except KeyboardInterrupt:
                    pass

    _execute(_run)


@intent_app.command("history")
def intent_history(
    limit: int = typer.Option(20, "--limit", "-l", help="Max items to return."),
) -> None:
    """Show the audit trail of past intent translations."""
    cfg = _get_config()

    async def _run() -> None:
        async with BrokerAPIClient(cfg) as client:
            items = await client.get_intent_history(limit=limit)
            if cfg.output_format == "json":
                display_json(items)
            else:
                display_intent_history(items)

    _execute(_run)


# ═══════════════════════════════════════════════════════════════════════════
# Resource commands
# ═══════════════════════════════════════════════════════════════════════════

@resources_app.command("list")
def resources_list(
    resource_type: str | None = typer.Option(
        None, "--type", "-t", help="Filter by resource type."
    ),
    state: str | None = typer.Option(
        None, "--state", "-s", help="Filter by state (PENDING, ACTIVE, …)."
    ),
) -> None:
    """List all managed resources."""
    cfg = _get_config()

    async def _run() -> None:
        async with BrokerAPIClient(cfg) as client:
            resources = await client.list_resources(
                resource_type=resource_type,
                state=state,
            )
            if cfg.output_format == "json":
                display_json(resources)
            else:
                display_resources_table(resources)

    _execute(_run)


@resources_app.command("show")
def resources_show(
    resource_id: str = typer.Argument(..., help="Resource ID to inspect."),
) -> None:
    """Show detailed information about a specific resource."""
    cfg = _get_config()

    async def _run() -> None:
        async with BrokerAPIClient(cfg) as client:
            resource = await client.get_resource(resource_id)
            if cfg.output_format == "json":
                display_json(resource)
            else:
                display_resource_detail(resource)

    _execute(_run)


@resources_app.command("delete")
def resources_delete(
    resource_id: str = typer.Argument(..., help="Resource ID to delete."),
    confirm: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip confirmation prompt.",
    ),
) -> None:
    """Decommission a managed resource."""
    cfg = _get_config()

    if not confirm:
        proceed = typer.confirm(
            f"⚠  Delete resource {resource_id}? This action cannot be undone."
        )
        if not proceed:
            raise typer.Abort()

    async def _run() -> None:
        async with BrokerAPIClient(cfg) as client:
            result = await client.delete_resource(resource_id)
            if cfg.output_format == "json":
                display_json(result)
            else:
                console.print(f"[osb.success]✅ Resource {resource_id} deleted.[/]")

    _execute(_run)


# ═══════════════════════════════════════════════════════════════════════════
# Events commands
# ═══════════════════════════════════════════════════════════════════════════

@events_app.command("watch")
def events_watch() -> None:
    """Stream real-time infrastructure events via SSE.

    Press Ctrl+C to disconnect.
    """
    cfg = _get_config()

    async def _run() -> None:
        async with BrokerAPIClient(cfg) as client:
            console.print(
                "[osb.brand]📡 Connected to event stream[/]  "
                "[osb.muted]Press Ctrl+C to stop.[/]\n"
            )
            try:
                async for event in client.stream_events():
                    if event.event == "heartbeat" or event.data == "heartbeat":
                        if cfg.verbose:
                            display_sse_heartbeat()
                        continue

                    data = event.json_data
                    if cfg.output_format == "json":
                        display_json(data)
                    else:
                        display_sse_event(data, event.event)
            except KeyboardInterrupt:
                console.print("\n[osb.muted]Disconnected from event stream.[/]")

    _execute(_run)


@events_app.command("metrics")
def events_metrics() -> None:
    """Retrieve and display real-time event bus pub/sub metrics."""
    cfg = _get_config()

    async def _run() -> None:
        async with BrokerAPIClient(cfg) as client:
            result = await client.get_metrics()
            if cfg.output_format == "json":
                display_json(result)
            else:
                display_metrics(result)

    _execute(_run)


# ═══════════════════════════════════════════════════════════════════════════
# Maintenance commands
# ═══════════════════════════════════════════════════════════════════════════

@maintenance_app.command("analyze")
def maintenance_analyze(
    repository_path: str = typer.Argument(
        ".",
        help="Path to the repository to analyze.",
    ),
    include: list[str] | None = typer.Option(
        None,
        "--include",
        "-i",
        help="File glob pattern to include (multiple allowed).",
    ),
    exclude: list[str] | None = typer.Option(
        None,
        "--exclude",
        "-e",
        help="File glob pattern to exclude (multiple allowed).",
    ),
) -> None:
    """Trigger codebase coupling analysis, drift alerts, and refactoring proposals."""
    cfg = _get_config()

    async def _run() -> None:
        async with BrokerAPIClient(cfg) as client:
            result = await client.analyze_codebase(
                repository_path=repository_path,
                include_patterns=include,
                exclude_patterns=exclude,
            )
            if cfg.output_format == "json":
                display_json(result)
            else:
                display_coupling_report(result)

    _execute(_run)


@maintenance_app.command("drift")
def maintenance_drift() -> None:
    """Retrieve detected architectural drift alerts."""
    cfg = _get_config()

    async def _run() -> None:
        async with BrokerAPIClient(cfg) as client:
            result = await client.get_drift_alerts()
            if cfg.output_format == "json":
                display_json(result)
            else:
                display_drift_alerts(result)

    _execute(_run)


@maintenance_app.command("proposals")
def maintenance_proposals() -> None:
    """List pending refactoring proposals."""
    cfg = _get_config()

    async def _run() -> None:
        async with BrokerAPIClient(cfg) as client:
            result = await client.list_proposals()
            if cfg.output_format == "json":
                display_json(result)
            else:
                display_proposals_table(result)

    _execute(_run)


@maintenance_app.command("approve")
def maintenance_approve(
    proposal_id: str = typer.Argument(
        ...,
        help="The ID of the refactoring proposal to approve.",
    ),
) -> None:
    """Approve a refactoring proposal to generate a branch and pull request."""
    cfg = _get_config()

    async def _run() -> None:
        async with BrokerAPIClient(cfg) as client:
            result = await client.approve_proposal(proposal_id)
            if cfg.output_format == "json":
                display_json(result)
            else:
                display_approved_proposal(result)

    _execute(_run)


# ═══════════════════════════════════════════════════════════════════════════
# Health command (top-level, not in a sub-group)
# ═══════════════════════════════════════════════════════════════════════════

@app.command("health")
def health_check(
    ready: bool = typer.Option(
        False,
        "--ready",
        "-r",
        help="Check the readiness probe (/health/ready) instead of liveness.",
    ),
) -> None:
    """Check the broker API health status."""
    cfg = _get_config()

    async def _run() -> None:
        async with BrokerAPIClient(cfg) as client:
            if ready:
                data = await client.health_ready()
                endpoint = "/health/ready"
            else:
                data = await client.health()
                endpoint = "/health"

            if cfg.output_format == "json":
                display_json(data)
            else:
                display_health(data, endpoint)

    _execute(_run)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _execute(coro_factory) -> None:  # type: ignore[no-untyped-def]
    """Run an async command, handling common errors gracefully."""
    cfg = _get_config()
    try:
        run_async(coro_factory())
    except BrokerConnectionError:
        display_connection_error(cfg.base_url)
        raise typer.Exit(1) from None
    except APIError as e:
        display_error(
            f"API error (HTTP {e.status_code})",
            e.detail,
        )
        raise typer.Exit(1) from None
    except KeyboardInterrupt:
        console.print("\n[osb.muted]Interrupted.[/]")
        raise typer.Exit(0) from None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    """Entry point for the ``osb`` console script."""
    display_banner()
    app()


if __name__ == "__main__":
    main()
