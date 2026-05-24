"""
broker-maintain — AI CI/CD Maintenance Agent CLI.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import Any

# Force UTF-8 on Windows so emojis render correctly and stdout doesn't hang
if sys.platform == "win32":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    os.environ.setdefault("PYTHONUTF8", "1")
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except AttributeError:
        pass

from broker.config import get_settings
from broker.services.github_integration import GitHubAdapter
from broker.services.maintenance_runner import MaintenanceRunner


def print_err(text: str) -> None:
    """Print to stderr."""
    print(text, file=sys.stderr)


async def run_analyze(args: argparse.Namespace) -> int:
    """Run analysis and print result."""
    settings = get_settings()
    runner = MaintenanceRunner(settings)

    print(f"Analyzing repository at '{args.repo_path}'...")
    result = await runner.run(args.repo_path, coupling_threshold=args.coupling_threshold, dry_run=True, create_pr=False)

    report = result["coupling_report"]
    alerts = result["drift_alerts"]

    if args.output == "json":
        import json
        out = {
            "average_coupling": report.average_coupling,
            "total_files": report.total_files,
            "hotspots": report.hotspots,
            "alerts": [
                {
                    "component": a.component,
                    "type": a.drift_type.value,
                    "severity": a.severity.value,
                    "description": a.description,
                }
                for a in alerts
            ]
        }
        print(json.dumps(out, indent=2))
        return result["severity_level"]

    if args.output == "markdown":
        print(result["report_md"])
        return result["severity_level"]

    # Human-readable output
    print("\n" + "=" * 60)
    print(" 📊 COUPLING ANALYSIS REPORT")
    print("=" * 60)
    print(f"Repository:               {report.repository}")
    print(f"Total files:              {report.total_files}")
    print(f"Average coupling score:    {report.average_coupling:.3f}")
    
    if report.hotspots:
        print("\n🔥 Hotspot Modules:")
        for h in report.hotspots[:5]:
            print(f"  - {h}")

    print("\n" + "=" * 60)
    print(" ⚠️ ARCHITECTURAL DRIFT ALERTS")
    print("=" * 60)
    if not alerts:
        print("✅ No architectural drift or god modules detected.")
    else:
        for a in alerts:
            print(f"[{a.severity.value}] {a.drift_type.value} in {a.component}")
            print(f"  ↳ Description: {a.description}")
            print(f"  ↳ Suggested:   {a.suggested_action}\n")

    return result["severity_level"]


async def run_propose(args: argparse.Namespace) -> int:
    """Generate refactoring proposals."""
    settings = get_settings()
    runner = MaintenanceRunner(settings)

    print(f"Generating refactoring proposals for '{args.repo_path}'...")
    result = await runner.run(args.repo_path, dry_run=True, create_pr=False)

    proposals = result["proposals"]

    if args.output == "json":
        import json
        out = [
            {
                "proposal_id": p.proposal_id,
                "title": p.title,
                "description": p.description,
                "files_affected": p.files_affected,
                "confidence": p.confidence,
                "estimated_effort": p.estimated_effort,
            }
            for p in proposals
        ]
        print(json.dumps(out, indent=2))
        return 0

    print("\n" + "=" * 60)
    print(" 🧠 SUGGESTED REFACTORING PROPOSALS")
    print("=" * 60)
    if not proposals:
        print("No coupling issues exceeded thresholds. No proposals generated.")
    else:
        for p in proposals:
            print(f"💡 {p.title} (Confidence: {p.confidence * 100:.1f}%)")
            print(f"   Description: {p.description}")
            print(f"   Effort:      {p.estimated_effort}")
            print(f"   Affected:    {', '.join(p.files_affected)}")
            if p.diff_preview:
                print("   Diff Preview:")
                for line in p.diff_preview.splitlines()[:5]:
                    print(f"     {line}")
                if len(p.diff_preview.splitlines()) > 5:
                    print("     ...")
            print()

    return 0


async def run_pr(args: argparse.Namespace) -> int:
    """Open a PR containing the report."""
    settings = get_settings()
    dry_run = not args.no_dry_run

    # Override token / repo if provided
    repo = args.repo or os.environ.get("GITHUB_REPO", "")
    token = args.token or os.environ.get("GITHUB_TOKEN", "")

    if not dry_run:
        if not repo:
            print_err("Error: --repo or GITHUB_REPO environment variable must be set.")
            return 1
        if not token:
            print_err("Error: --token or GITHUB_TOKEN environment variable must be set.")
            return 1

    git_provider = GitHubAdapter(repo_name=repo, token=token, dry_run=dry_run)
    runner = MaintenanceRunner(settings, git_provider=git_provider)

    action_label = "DRY RUN: Preparing" if dry_run else "Creating"
    print(f"{action_label} pull request for repository '{args.repo_path}'...")
    
    result = await runner.run(args.repo_path, dry_run=dry_run, create_pr=True)
    
    if result["pr_url"]:
        print(f"Successfully processed Git workflow! PR URL: {result['pr_url']}")
    else:
        print("No drift or coupling issues detected. Skipping PR creation.")

    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI Entrypoint parser."""
    parser = argparse.ArgumentParser(
        prog="broker-maintain",
        description="AI CI/CD Maintenance Agent — monitors codebase health and suggests refactorings.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # analyze subcommand
    parser_analyze = subparsers.add_parser("analyze", help="Run coupling and drift analysis.")
    parser_analyze.add_argument("repo_path", nargs="?", default=".", help="Path to repository (default: .)")
    parser_analyze.add_argument("--coupling-threshold", type=float, default=0.5, help="Threshold for high coupling (default: 0.5)")
    parser_analyze.add_argument("--output", choices=["human", "json", "markdown"], default="human", help="Output format (default: human)")

    # propose subcommand
    parser_propose = subparsers.add_parser("propose", help="Generate refactoring proposals.")
    parser_propose.add_argument("repo_path", nargs="?", default=".", help="Path to repository (default: .)")
    parser_propose.add_argument("--output", choices=["human", "json"], default="human", help="Output format (default: human)")

    # pr subcommand
    parser_pr = subparsers.add_parser("pr", help="Publish maintenance audit report to a GitHub PR.")
    parser_pr.add_argument("repo_path", nargs="?", default=".", help="Path to repository (default: .)")
    parser_pr.add_argument("--no-dry-run", action="store_true", help="Perform write operations on GitHub (default is dry-run).")
    parser_pr.add_argument("--repo", help="Target GitHub repo 'owner/repo' (overrides GITHUB_REPO env var).")
    parser_pr.add_argument("--token", help="GitHub Personal Access Token (overrides GITHUB_TOKEN env var).")

    parsed_args = parser.parse_args(argv)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        if parsed_args.command == "analyze":
            return loop.run_until_complete(run_analyze(parsed_args))
        elif parsed_args.command == "propose":
            return loop.run_until_complete(run_propose(parsed_args))
        elif parsed_args.command == "pr":
            return loop.run_until_complete(run_pr(parsed_args))
    except Exception as e:
        print_err(f"Fatal execution error: {e}")
        return 255
    finally:
        loop.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
