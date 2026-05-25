"""
Rich display helpers for the ``osb`` CLI.

Every user-facing output is rendered through this module so the CLI has a
consistent, premium visual identity.  All helpers accept plain ``dict``
payloads (as returned by ``BrokerAPIClient``) — they do **not** depend on
Pydantic models so the CLI package stays decoupled from the server schemas.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from typing import Any

# Force UTF-8 on Windows so Rich's box-drawing and emoji render correctly
if sys.platform == "win32":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    os.environ.setdefault("PYTHONUTF8", "1")
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except AttributeError:
        pass


from rich.console import Console
from rich.json import JSON
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

# ---------------------------------------------------------------------------
# Theme
# ---------------------------------------------------------------------------
_THEME = Theme(
    {
        "osb.brand": "bold bright_cyan",
        "osb.success": "bold green",
        "osb.warning": "bold yellow",
        "osb.error": "bold red",
        "osb.muted": "dim",
        "osb.key": "bold bright_white",
        "osb.value": "bright_cyan",
        "osb.safe": "green",
        "osb.risky": "red",
        "osb.state.pending": "yellow",
        "osb.state.provisioning": "bright_cyan",
        "osb.state.active": "bold green",
        "osb.state.deprovisioning": "bright_yellow",
        "osb.state.failed": "bold red",
        "osb.state.deleted": "dim",
    }
)

console = Console(theme=_THEME, force_terminal=True)
err_console = Console(stderr=True, theme=_THEME, force_terminal=True)


# ---------------------------------------------------------------------------
# State badge helper
# ---------------------------------------------------------------------------
_STATE_ICONS: dict[str, str] = {
    "PENDING": "⏳",
    "PROVISIONING": "🔄",
    "ACTIVE": "✅",
    "DEPROVISIONING": "🔻",
    "FAILED": "❌",
    "DELETED": "🗑️",
}


def _state_badge(state: str) -> Text:
    """Return a coloured state badge with icon."""
    icon = _STATE_ICONS.get(state, "❓")
    style = f"osb.state.{state.lower()}" if f"osb.state.{state.lower()}" in _THEME.styles else ""
    return Text(f"{icon} {state}", style=style)


# ---------------------------------------------------------------------------
# Risk score bar
# ---------------------------------------------------------------------------
def _risk_bar(score: float, width: int = 20) -> Text:
    """Render a colour-coded risk score bar."""
    filled = int(score * width)
    empty = width - filled

    if score <= 0.3:
        colour = "green"
        label = "LOW"
    elif score <= 0.6:
        colour = "yellow"
        label = "MEDIUM"
    else:
        colour = "red"
        label = "HIGH"

    bar = Text()
    bar.append("█" * filled, style=colour)
    bar.append("░" * empty, style="dim")
    bar.append(f"  {score:.0%} {label}", style=f"bold {colour}")
    return bar


# ---------------------------------------------------------------------------
# Confidence bar
# ---------------------------------------------------------------------------
def _confidence_bar(score: float, width: int = 20) -> Text:
    """Render a colour-coded confidence bar."""
    filled = int(score * width)
    empty = width - filled

    if score >= 0.85:
        colour = "green"
    elif score >= 0.6:
        colour = "yellow"
    else:
        colour = "red"

    bar = Text()
    bar.append("━" * filled, style=f"bold {colour}")
    bar.append("╌" * empty, style="dim")
    bar.append(f"  {score:.0%}", style=f"bold {colour}")
    return bar


# ---------------------------------------------------------------------------
# Intent parse response
# ---------------------------------------------------------------------------
def display_intent_response(data: dict[str, Any]) -> None:
    """Display the result of ``/api/v1/intent/parse`` as a rich panel."""
    parsed = data.get("parsed_configuration", {})
    validation = data.get("validation", {})
    blast = data.get("blast_radius", {})
    confidence = data.get("confidence_score", 0.0)
    warnings = data.get("warnings", [])
    request_id = data.get("request_id", "—")

    # ── Header section ────────────────────────────────────────────
    header = Table.grid(padding=(0, 2))
    header.add_column(style="osb.key", min_width=14)
    header.add_column(style="osb.value")
    header.add_row("Request ID", request_id)
    header.add_row("Action", parsed.get("action", "—"))
    header.add_row("Target", parsed.get("target_service", "—"))
    header.add_row("Confidence", _confidence_bar(confidence))
    header.add_row("Reasoning", parsed.get("reasoning", "—"))

    # ── Configuration JSON ────────────────────────────────────────
    config_json = JSON(json.dumps(parsed.get("parameters", {}), indent=2))

    # ── Validation section ────────────────────────────────────────
    is_valid = validation.get("is_valid", False)
    val_icon = "✅" if is_valid else "❌"
    val_style = "osb.success" if is_valid else "osb.error"

    val_table = Table.grid(padding=(0, 2))
    val_table.add_column(style="osb.key", min_width=14)
    val_table.add_column()
    val_table.add_row("Validation", Text(f"{val_icon} {'PASSED' if is_valid else 'FAILED'}", style=val_style))

    for err in validation.get("errors", []):
        val_table.add_row("", Text(f"  ✗ {err}", style="osb.error"))

    # ── Blast radius section ──────────────────────────────────────
    risk_score = blast.get("risk_score", 0.0)
    is_safe = blast.get("is_safe", True)
    safe_icon = "✅ SAFE" if is_safe else "⛔ UNSAFE"
    safe_style = "osb.safe" if is_safe else "osb.risky"

    blast_table = Table.grid(padding=(0, 2))
    blast_table.add_column(style="osb.key", min_width=14)
    blast_table.add_column()
    blast_table.add_row("Risk Score", _risk_bar(risk_score))
    blast_table.add_row("Verdict", Text(safe_icon, style=safe_style))

    affected = blast.get("affected_services", [])
    if affected:
        blast_table.add_row("Affected", Text(", ".join(affected), style="osb.value"))

    desc = blast.get("description", "")
    if desc:
        blast_table.add_row("Details", Text(desc, style="osb.muted"))

    # ── Warnings section ──────────────────────────────────────────
    warning_lines = Text()
    for w in warnings:
        warning_lines.append(f"  ⚠  {w}\n", style="osb.warning")

    # ── Assemble panel ────────────────────────────────────────────
    from rich.console import Group

    sections: list[Any] = [
        header,
        Text(""),
        Panel(config_json, title="Parsed Parameters", border_style="bright_cyan", padding=(0, 1)),
        Text(""),
        val_table,
        Text(""),
        blast_table,
    ]

    if warnings:
        sections.append(Text(""))
        sections.append(Panel(warning_lines, title="⚠ Warnings", border_style="yellow", padding=(0, 1)))

    console.print(
        Panel(
            Group(*sections),
            title="[osb.brand]🧠 AI Intent Parser[/]",
            subtitle=f"[osb.muted]{request_id}[/]",
            border_style="bright_cyan",
            padding=(1, 2),
        )
    )


# ---------------------------------------------------------------------------
# Apply response
# ---------------------------------------------------------------------------
def display_apply_response(data: dict[str, Any]) -> None:
    """Display the result of ``/api/v1/intent/apply``."""
    table = Table.grid(padding=(0, 2))
    table.add_column(style="osb.key", min_width=14)
    table.add_column(style="osb.value")
    table.add_row("Status", Text("✅ ACCEPTED", style="osb.success"))
    table.add_row("Request ID", data.get("request_id", "—"))
    table.add_row("Resource ID", data.get("resource_id", "—"))
    table.add_row("Message", data.get("message", "—"))

    console.print(
        Panel(
            table,
            title="[osb.brand]🚀 Configuration Applied[/]",
            border_style="green",
            padding=(1, 2),
        )
    )


# ---------------------------------------------------------------------------
# Intent history
# ---------------------------------------------------------------------------
def display_intent_history(items: list[dict[str, Any]]) -> None:
    """Display intent translation history as a rich table."""
    if not items:
        console.print("[osb.muted]No intent history found.[/]")
        return

    table = Table(
        title="📜 Intent History",
        title_style="osb.brand",
        show_lines=False,
        border_style="bright_cyan",
        header_style="bold bright_white",
        padding=(0, 1),
    )
    table.add_column("Request ID", style="osb.muted", max_width=20)
    table.add_column("Action", style="osb.value")
    table.add_column("Target", style="osb.value")
    table.add_column("Status")
    table.add_column("Created", style="osb.muted")

    for item in items:
        status = item.get("status", "—").upper()
        table.add_row(
            item.get("request_id", "—")[:20],
            item.get("action", "—"),
            item.get("target_service", "—"),
            _state_badge(status),
            _format_timestamp(item.get("created_at")),
        )

    console.print(table)


# ---------------------------------------------------------------------------
# Resources table
# ---------------------------------------------------------------------------
def display_resources_table(resources: list[dict[str, Any]]) -> None:
    """Display a list of resources as a coloured table."""
    if not resources:
        console.print("[osb.muted]No resources found.[/]")
        return

    table = Table(
        title="📦 Managed Resources",
        title_style="osb.brand",
        show_lines=False,
        border_style="bright_cyan",
        header_style="bold bright_white",
        padding=(0, 1),
    )
    table.add_column("Resource ID", style="osb.muted", max_width=24)
    table.add_column("Type", style="osb.value")
    table.add_column("State")
    table.add_column("Version", justify="right", style="osb.muted")
    table.add_column("Created", style="osb.muted")
    table.add_column("Updated", style="osb.muted")

    for r in resources:
        state = r.get("state", "—").upper()
        table.add_row(
            r.get("resource_id", "—")[:24],
            r.get("resource_type", "—"),
            _state_badge(state),
            str(r.get("version", "—")),
            _format_timestamp(r.get("created_at")),
            _format_timestamp(r.get("updated_at")),
        )

    console.print(table)


# ---------------------------------------------------------------------------
# Resource detail
# ---------------------------------------------------------------------------
def display_resource_detail(resource: dict[str, Any]) -> None:
    """Display a single resource in a rich panel."""
    state = resource.get("state", "—").upper()

    info = Table.grid(padding=(0, 2))
    info.add_column(style="osb.key", min_width=16)
    info.add_column()
    info.add_row("Resource ID", Text(resource.get("resource_id", "—"), style="osb.value"))
    info.add_row("Type", Text(resource.get("resource_type", "—"), style="osb.value"))
    info.add_row("State", _state_badge(state))
    info.add_row("Version", Text(str(resource.get("version", "—")), style="osb.value"))
    info.add_row("Created By", Text(resource.get("created_by", "—"), style="osb.muted"))
    info.add_row("Created At", Text(_format_timestamp(resource.get("created_at")), style="osb.muted"))
    info.add_row("Updated At", Text(_format_timestamp(resource.get("updated_at")), style="osb.muted"))

    error_msg = resource.get("error_message")
    if error_msg:
        info.add_row("Error", Text(error_msg, style="osb.error"))

    from rich.console import Group

    sections: list[Any] = [info]

    config = resource.get("configuration", {})
    if config:
        sections.append(Text(""))
        sections.append(
            Panel(
                JSON(json.dumps(config, indent=2)),
                title="Configuration",
                border_style="bright_cyan",
                padding=(0, 1),
            )
        )

    console.print(
        Panel(
            Group(*sections),
            title=f"[osb.brand]📦 Resource — {resource.get('resource_id', '?')}[/]",
            border_style="bright_cyan",
            padding=(1, 2),
        )
    )


# ---------------------------------------------------------------------------
# SSE event display
# ---------------------------------------------------------------------------
def display_sse_event(event_data: dict[str, Any], event_type: str = "message") -> None:
    """Display a single SSE event in a compact, coloured format."""
    ts = _format_timestamp(event_data.get("timestamp"))
    resource_id = event_data.get("resource_id", "—")
    state = event_data.get("state", "")

    line = Text()
    line.append(f"  {ts}  ", style="osb.muted")
    line.append(f"[{event_type}]", style="bold bright_magenta")
    line.append("  ")
    line.append(resource_id, style="osb.value")

    if state:
        line.append("  →  ")
        line.append_text(_state_badge(state.upper()))

    data = event_data.get("data", {})
    if data:
        extra = ", ".join(f"{k}={v}" for k, v in data.items())
        if extra:
            line.append(f"  ({extra})", style="osb.muted")

    console.print(line)


def display_sse_heartbeat() -> None:
    """Display a subtle heartbeat indicator."""
    console.print("  [osb.muted]·[/]", end="")


# ---------------------------------------------------------------------------
# Health display
# ---------------------------------------------------------------------------
def display_health(data: dict[str, Any], endpoint: str = "/health") -> None:
    """Display the health check response."""
    status = data.get("status", "unknown")
    is_ok = status in ("ok", "healthy", "ready")

    icon = "✅" if is_ok else "❌"
    style = "osb.success" if is_ok else "osb.error"

    table = Table.grid(padding=(0, 2))
    table.add_column(style="osb.key", min_width=14)
    table.add_column()
    table.add_row("Endpoint", Text(endpoint, style="osb.value"))
    table.add_row("Status", Text(f"{icon} {status.upper()}", style=style))

    # Show extra detail keys
    for key, value in data.items():
        if key == "status":
            continue
        if key == "checks" and isinstance(value, dict):
            for sub_key, sub_val in value.items():
                table.add_row(
                    f"  ├─ {sub_key.title()}",
                    Text(str(sub_val), style="osb.success" if sub_val == "ok" else "osb.error"),
                )
        else:
            table.add_row(
                key.replace("_", " ").title(),
                Text(str(value), style="osb.value"),
            )

    console.print(
        Panel(
            table,
            title="[osb.brand]💊 Health Check[/]",
            border_style="green" if is_ok else "red",
            padding=(1, 2),
        )
    )


# ---------------------------------------------------------------------------
# Maintenance display helpers
# ---------------------------------------------------------------------------
def display_coupling_report(report: dict[str, Any]) -> None:
    """Display the result of code coupling analysis."""
    from rich.console import Group

    repo = report.get("repository", "—")
    total_files = report.get("total_files", 0)
    total_modules = report.get("total_modules", 0)
    avg_coupling = report.get("average_coupling", 0.0)
    hotspots = report.get("hotspots", [])
    recommendations = report.get("recommendations", [])
    file_couplings = report.get("file_couplings", [])

    info = Table.grid(padding=(0, 2))
    info.add_column(style="osb.key", min_width=16)
    info.add_column()
    info.add_row("Repository", Text(repo, style="osb.value"))
    info.add_row("Total Files", Text(str(total_files), style="osb.value"))
    info.add_row("Total Modules", Text(str(total_modules), style="osb.value"))
    info.add_row(
        "Avg Coupling",
        Text(
            f"{avg_coupling:.2%}",
            style="osb.success" if avg_coupling < 0.3 else "osb.warning" if avg_coupling < 0.6 else "osb.error",
        ),
    )

    sections: list[Any] = [info]

    # Coupling details table if there are couplings
    if file_couplings:
        table = Table(
            title="File Coupling Details",
            title_style="bold bright_white",
            show_lines=False,
            border_style="dim",
            header_style="bold bright_white",
            padding=(0, 1),
        )
        table.add_column("File A", style="osb.muted")
        table.add_column("File B", style="osb.muted")
        table.add_column("Score", justify="right")
        table.add_column("Shared Symbols", style="osb.value")

        # Show top 5 couplings
        for c in file_couplings[:5]:
            score = c.get("coupling_score", 0.0)
            score_style = "green" if score < 0.3 else "yellow" if score < 0.6 else "red"
            symbols = ", ".join(c.get("shared_symbols", []))
            table.add_row(
                c.get("file_a", ""),
                c.get("file_b", ""),
                Text(f"{score:.0%}", style=score_style),
                symbols or "—",
            )

        sections.append(Text(""))
        sections.append(table)
        if len(file_couplings) > 5:
            sections.append(
                Text(f"  ... and {len(file_couplings) - 5} more coupling pairs.", style="osb.muted")
            )

    if hotspots:
        sections.append(Text(""))
        hotspots_text = Text()
        for h in hotspots:
            hotspots_text.append(f"  🔥 {h}\n", style="osb.risky")
        sections.append(
            Panel(hotspots_text, title="Hotspots (Highly Coupled)", border_style="red", padding=(0, 1))
        )

    if recommendations:
        sections.append(Text(""))
        rec_text = Text()
        for r in recommendations:
            rec_text.append(f"  💡 {r}\n", style="osb.brand")
        sections.append(
            Panel(rec_text, title="Recommendations", border_style="bright_cyan", padding=(0, 1))
        )

    console.print(
        Panel(
            Group(*sections),
            title="[osb.brand]🛠️ Codebase Coupling Analysis[/]",
            border_style="bright_cyan",
            padding=(1, 2),
        )
    )


def display_drift_alerts(alerts: list[dict[str, Any]]) -> None:
    """Display architectural drift alerts as a rich table."""
    if not alerts:
        console.print("[osb.success]✅ No architectural drift detected.[/]")
        return

    table = Table(
        title="🚨 Architectural Drift Alerts",
        title_style="osb.error",
        show_lines=False,
        border_style="red",
        header_style="bold bright_white",
        padding=(0, 1),
    )
    table.add_column("ID", style="osb.muted")
    table.add_column("Component", style="osb.value")
    table.add_column("Type", style="osb.value")
    table.add_column("Severity")
    table.add_column("Description")
    table.add_column("Suggested Action", style="osb.success")

    _severity_styles: dict[str, str] = {
        "LOW": "green",
        "MEDIUM": "yellow",
        "HIGH": "bold red",
        "CRITICAL": "bold red reverse",
    }

    for alert in alerts:
        severity = alert.get("severity", "LOW").upper()
        sev_style = _severity_styles.get(severity, "bright_white")
        table.add_row(
            alert.get("alert_id", "—"),
            alert.get("component", "—"),
            alert.get("drift_type", "—"),
            Text(severity, style=sev_style),
            alert.get("description", "—"),
            alert.get("suggested_action", "—"),
        )

    console.print(table)


def display_proposals_table(proposals: list[dict[str, Any]]) -> None:
    """Display pending refactoring proposals."""
    if not proposals:
        console.print("[osb.muted]No pending refactoring proposals found.[/]")
        return

    table = Table(
        title="💡 Pending Refactor Proposals",
        title_style="osb.brand",
        show_lines=False,
        border_style="bright_cyan",
        header_style="bold bright_white",
        padding=(0, 1),
    )
    table.add_column("Proposal ID", style="osb.muted", max_width=20)
    table.add_column("Title", style="osb.value")
    table.add_column("Affected Files", style="osb.muted")
    table.add_column("Confidence", justify="right")
    table.add_column("Effort", style="osb.value")
    table.add_column("Status")

    for p in proposals:
        confidence = p.get("confidence", 0.0)
        conf_style = "green" if confidence >= 0.8 else "yellow" if confidence >= 0.6 else "red"
        files = ", ".join(p.get("files_affected", []))
        status = p.get("status", "pending").upper()

        if status == "APPROVED":
            status_text = Text("APPROVED", style="osb.success")
        elif status == "PENDING":
            status_text = Text("PENDING", style="osb.warning")
        else:
            status_text = Text(status, style="osb.muted")

        table.add_row(
            p.get("proposal_id", "—")[:20],
            p.get("title", "—"),
            files or "—",
            Text(f"{confidence:.0%}", style=conf_style),
            p.get("estimated_effort", "—"),
            status_text,
        )

    console.print(table)


def display_approved_proposal(proposal: dict[str, Any]) -> None:
    """Display details of an approved refactor proposal."""
    from rich.console import Group
    from rich.syntax import Syntax

    title = proposal.get("title", "—")
    proposal_id = proposal.get("proposal_id", "—")
    desc = proposal.get("description", "—")
    files = ", ".join(proposal.get("files_affected", []))
    confidence = proposal.get("confidence", 0.0)
    effort = proposal.get("estimated_effort", "—")
    diff = proposal.get("diff_preview", "")

    info = Table.grid(padding=(0, 2))
    info.add_column(style="osb.key", min_width=16)
    info.add_column()
    info.add_row("Proposal ID", Text(proposal_id, style="osb.value"))
    info.add_row("Title", Text(title, style="osb.value"))
    info.add_row("Description", Text(desc))
    info.add_row("Affected Files", Text(files, style="osb.value"))
    info.add_row("Confidence", _confidence_bar(confidence))
    info.add_row("Est. Effort", Text(effort, style="osb.value"))

    sections: list[Any] = [info]

    if diff:
        syntax_diff = Syntax(diff, "diff", theme="ansi_dark", word_wrap=True)
        sections.append(Text(""))
        sections.append(
            Panel(
                syntax_diff,
                title="Proposed Changes Diff",
                border_style="bright_cyan",
                padding=(0, 1),
            )
        )

    console.print(
        Panel(
            Group(*sections),
            title="[osb.success]✅ Refactor Proposal Approved for Background PR Generation[/]",
            border_style="green",
            padding=(1, 2),
        )
    )





def display_metrics(metrics: dict[str, Any]) -> None:
    """Display real-time pub/sub metrics in a visual panel."""

    parse_success = metrics.get("intent_parse_success", 0)
    parse_failed = metrics.get("intent_parse_failed", 0)
    provision_success = metrics.get("provision_success", 0)
    provision_failed = metrics.get("provision_failed", 0)

    total_parses = parse_success + parse_failed
    total_provisions = provision_success + provision_failed

    def make_percentage_bar(success: int, failed: int) -> Text:
        total = success + failed
        if total == 0:
            return Text("—", style="osb.muted")
        success_pct = success / total
        width = 20
        filled = int(success_pct * width)
        empty = width - filled
        bar = Text()
        bar.append("█" * filled, style="green")
        bar.append("█" * empty, style="red")
        bar.append(f"  {success_pct:.0%} Success", style="bold green" if success_pct >= 0.8 else "bold yellow")
        return bar

    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="osb.key", min_width=22)
    grid.add_column()

    grid.add_row("Total Intent Parses", Text(str(total_parses), style="osb.value"))
    grid.add_row("  ├─ Successful", Text(str(parse_success), style="osb.success"))
    grid.add_row("  └─ Failed", Text(str(parse_failed), style="osb.error" if parse_failed > 0 else "osb.muted"))
    grid.add_row("Parse Ratio", make_percentage_bar(parse_success, parse_failed))
    grid.add_row("", "")
    grid.add_row("Total Provisions", Text(str(total_provisions), style="osb.value"))
    grid.add_row("  ├─ Successful", Text(str(provision_success), style="osb.success"))
    grid.add_row("  └─ Failed", Text(str(provision_failed), style="osb.error" if provision_failed > 0 else "osb.muted"))
    grid.add_row("Provision Ratio", make_percentage_bar(provision_success, provision_failed))

    console.print(
        Panel(
            grid,
            title="[osb.brand]📡 Real-Time Pub/Sub Metrics[/]",
            border_style="bright_cyan",
            padding=(1, 2),
        )
    )


# ---------------------------------------------------------------------------
# Error display
# ---------------------------------------------------------------------------
def display_error(message: str, detail: str | None = None) -> None:
    """Display an error message in a red panel."""
    body = Text(message, style="osb.error")
    if detail:
        body.append(f"\n\n{detail}", style="osb.muted")

    err_console.print(
        Panel(
            body,
            title="[osb.error]✗ Error[/]",
            border_style="red",
            padding=(1, 2),
        )
    )


def display_connection_error(base_url: str) -> None:
    """Display a user-friendly connection error."""
    from rich.console import Group

    err_console.print(
        Panel(
            Group(
                Text("Cannot connect to the broker API", style="osb.error"),
                Text(""),
                Text(f"  URL:  {base_url}", style="osb.value"),
                Text(""),
                Text("Make sure the server is running:", style="osb.muted"),
                Text("  docker-compose up -d", style="bold bright_white"),
                Text("  — or —", style="osb.muted"),
                Text("  uvicorn broker.main:app --reload", style="bold bright_white"),
            ),
            title="[osb.error]⚡ Connection Failed[/]",
            border_style="red",
            padding=(1, 2),
        )
    )


# ---------------------------------------------------------------------------
# JSON fallback
# ---------------------------------------------------------------------------
def display_json(data: Any) -> None:
    """Pretty-print raw JSON (for ``--json`` output mode)."""
    console.print_json(json.dumps(data, indent=2, default=str))


# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------
def display_banner() -> None:
    """Print the CLI banner."""
    banner = Text()
    banner.append("  ╭───────────────────────────────────────╮\n", style="bright_cyan")
    banner.append("  │", style="bright_cyan")
    banner.append("  ◆ Open Service Broker CLI  ", style="bold bright_white")
    banner.append("v0.1.0", style="osb.muted")
    banner.append("  │\n", style="bright_cyan")
    banner.append("  ╰───────────────────────────────────────╯", style="bright_cyan")
    console.print(banner)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------
def _format_timestamp(ts: Any) -> str:
    """Format an ISO timestamp to a compact human-readable string."""
    if ts is None:
        return "—"
    if isinstance(ts, str):
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return dt.strftime("%Y-%m-%d %H:%M")
        except (ValueError, TypeError):
            return ts[:16] if len(ts) > 16 else ts
    if isinstance(ts, datetime):
        return ts.strftime("%Y-%m-%d %H:%M")
    return str(ts)
