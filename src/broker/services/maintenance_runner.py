"""
Maintenance Runner — orchestrates coupling analysis, drift detection, refactoring, and PRs.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

from broker.schemas.maintenance import CouplingReport, DriftAlert, DriftSeverity, RefactorProposal
from broker.services.code_analyzer import CodeAnalyzer
from broker.services.github_integration import GitHubAdapter, GitProviderAdapter
from broker.services.llm_gateway import create_llm_gateway
from broker.services.refactoring_agent import RefactoringAgent

if TYPE_CHECKING:
    from broker.config import Settings

logger = structlog.get_logger()


class MaintenanceRunner:
    """Ties together CodeAnalyzer, RefactoringAgent, and GitProviderAdapter."""

    def __init__(
        self,
        settings: Settings,
        git_provider: GitProviderAdapter | None = None,
    ) -> None:
        self.settings = settings
        self.analyzer = CodeAnalyzer()
        self.llm_gateway = create_llm_gateway(settings)
        self.agent = RefactoringAgent(self.llm_gateway)
        self.git_provider = git_provider or GitHubAdapter(dry_run=True)

    async def run(
        self,
        repo_path: str,
        coupling_threshold: float = 0.5,
        dry_run: bool = True,
        create_pr: bool = False,
    ) -> dict[str, Any]:
        """Run coupling analysis, detect architectural drift, generate proposals, and optionally open a PR."""
        await logger.ainfo("Starting maintenance run", repo_path=repo_path, dry_run=dry_run)

        # 1. Run coupling analysis
        coupling_report = self.analyzer.analyze_coupling(repo_path)

        # 2. Detect drift
        drift_alerts = self.analyzer.detect_drift(repo_path)

        # Filter couplings by threshold or generate proposals
        proposals = await self.agent.propose_refactors(coupling_report)

        # Determine severity level
        # 0 = clean, 1 = warning, 2 = critical
        severity_level = 0
        if drift_alerts:
            high_critical = [
                a for a in drift_alerts
                if a.severity in {DriftSeverity.HIGH, DriftSeverity.CRITICAL}
            ]
            severity_level = 2 if high_critical else 1

        # Generate markdown report
        report_md = self._generate_report_markdown(coupling_report, drift_alerts, proposals)

        pr_url = None
        if create_pr and (drift_alerts or proposals):
            # Configure git adapter dry run status
            if isinstance(self.git_provider, GitHubAdapter):
                self.git_provider.dry_run = dry_run

            timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
            branch_name = f"refactor/coupling-mitigation-{timestamp}"
            title = f"🔧 AI Maintenance: Coupling Mitigation ({timestamp})"

            # Commit the report as a documentation file
            file_changes = {
                "reports/coupling_report.md": report_md
            }

            try:
                await self.git_provider.create_branch(branch_name)
                await self.git_provider.commit_changes(
                    branch_name=branch_name,
                    commit_message="docs: add automated coupling and drift report",
                    file_changes=file_changes,
                )
                pr_url = await self.git_provider.create_pull_request(
                    branch_name=branch_name,
                    title=title,
                    body=self._generate_pr_body(coupling_report, drift_alerts, proposals),
                )
            except Exception as e:
                await logger.aerror("Failed to complete Git provider workflow", error=str(e))
                if not dry_run:
                    raise

        return {
            "coupling_report": coupling_report,
            "drift_alerts": drift_alerts,
            "proposals": proposals,
            "severity_level": severity_level,
            "report_md": report_md,
            "pr_url": pr_url,
        }

    def _generate_report_markdown(
        self,
        report: CouplingReport,
        alerts: list[DriftAlert],
        proposals: list[RefactorProposal],
    ) -> str:
        """Generate a complete Markdown report of coupling and drift analysis."""
        lines = [
            "# 🔧 Automated Codebase Coupling & Drift Report",
            f"Generated at: {datetime.now(UTC).isoformat()}",
            f"Repository: `{report.repository}`",
            "",
            "## 📊 Summary Metrics",
            f"- **Total files analyzed**: {report.total_files}",
            f"- **Total modules detected**: {report.total_modules}",
            f"- **Average coupling score**: `{report.average_coupling:.3f}`",
            "",
        ]

        if report.hotspots:
            lines.append("### 🔥 Hotspots")
            for h in report.hotspots[:5]:
                lines.append(f"- `{h}`")
            lines.append("")

        lines.extend([
            "## ⚠️ Architectural Drift Alerts",
        ])
        if not alerts:
            lines.append("No architectural drift or god modules detected. Codebase matches baseline conventions! ✅")
        else:
            for alert in alerts:
                lines.append(
                    f"### [{alert.severity.value}] {alert.drift_type.value} in `{alert.component}`"
                )
                lines.append(f"- **Description**: {alert.description}")
                lines.append(f"- **Suggested Action**: {alert.suggested_action}")
                lines.append("")

        lines.extend([
            "",
            "## 🧠 Suggested Refactoring Proposals",
        ])
        if not proposals:
            lines.append("No coupling issues exceeded thresholds. No refactoring proposals generated. 🎉")
        else:
            for p in proposals:
                lines.append(f"### {p.title} (Confidence: {p.confidence * 100:.1f}%)")
                lines.append(f"- **Description**: {p.description}")
                lines.append(f"- **Effort level**: {p.estimated_effort}")
                lines.append(f"- **Affected Files**: {', '.join(f'`{f}`' for f in p.files_affected)}")
                if p.diff_preview:
                    lines.append("```diff")
                    lines.append(p.diff_preview)
                    lines.append("```")
                lines.append("")

        return "\n".join(lines)

    def _generate_pr_body(
        self,
        report: CouplingReport,
        alerts: list[DriftAlert],
        proposals: list[RefactorProposal],
    ) -> str:
        """Generate a pull request description summarizing the changes and analysis."""
        body = [
            "## 🔧 Advanced AI Service Broker — Automated Maintenance Report",
            "This Pull Request registers a weekly codebase coupling and architectural drift audit report.",
            "",
            "### 📊 Key Coupling Metrics",
            f"- **Average coupling score**: `{report.average_coupling:.3f}`",
            f"- **Active alerts**: {len(alerts)} items flagged",
            f"- **Proposals generated**: {len(proposals)} recommended improvements",
            "",
            "### ⚠️ Architectural Alerts summary",
        ]

        for alert in alerts[:5]:
            body.append(f"- **[{alert.severity}]** {alert.drift_type}: `{alert.component}` — *{alert.description}*")
        if len(alerts) > 5:
            body.append(f"- *...and {len(alerts) - 5} more alerts.*")

        body.extend([
            "",
            "### 🧠 Proposed Refactorings",
        ])
        for p in proposals[:3]:
            body.append(f"- **{p.title}** ({p.estimated_effort} effort) — *{p.description}*")
        if len(proposals) > 3:
            body.append(f"- *...and {len(proposals) - 3} more proposals.*")

        body.extend([
            "",
            "---",
            "*Report saved to `reports/coupling_report.md`. Merge this PR to keep history in git.*"
        ])
        return "\n".join(body)
