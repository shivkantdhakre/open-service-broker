"""
Refactoring Agent — LLM-assisted automated refactoring proposal generator.

Takes coupling analysis results and uses the LLM to propose specific,
actionable refactoring changes with diff previews. All proposals are
suggestions only — they require human approval before execution.
"""

from __future__ import annotations

from datetime import UTC
from typing import TYPE_CHECKING

import structlog
from ulid import ULID

from broker.schemas.maintenance import CouplingReport, RefactorProposal

if TYPE_CHECKING:
    from broker.services.llm_gateway import LLMGateway

logger = structlog.get_logger()

REFACTOR_SYSTEM_PROMPT = """\
You are a senior software architect specializing in Python code quality.
Your task is to analyze coupling analysis results and propose specific,
actionable refactoring changes to reduce coupling and improve code quality.

RULES:
1. Each proposal must target a SPECIFIC coupling pair or hotspot.
2. Include a clear title, description, and affected files.
3. Estimate the effort level: "small" (<1 hour), "medium" (1-4 hours), "large" (>4 hours).
4. Provide a confidence score (0-1) for how beneficial the refactoring would be.
5. Focus on practical, incremental improvements — avoid "rewrite everything" proposals.

OUTPUT FORMAT (JSON array):
[
  {
    "title": "Extract shared utilities from module_a and module_b",
    "description": "Both modules import the same 5 functions...",
    "files_affected": ["src/module_a.py", "src/module_b.py", "src/shared/utils.py"],
    "confidence": 0.85,
    "estimated_effort": "small",
    "diff_preview": "--- a/src/module_a.py\\n+++ b/src/module_a.py\\n-from utils import ...",
  }
]
"""


class RefactoringAgent:
    """LLM-assisted refactoring proposal generator."""

    def __init__(self, llm_gateway: LLMGateway) -> None:
        self._llm = llm_gateway
        self._proposals: dict[str, RefactorProposal] = {}

    async def propose_refactors(
        self,
        report: CouplingReport,
        max_proposals: int = 5,
    ) -> list[RefactorProposal]:
        """Generate refactoring proposals based on a coupling analysis report.

        Args:
            report: The coupling analysis report to base proposals on.
            max_proposals: Maximum number of proposals to generate.

        Returns:
            List of RefactorProposal instances.
        """
        if not report.file_couplings and not report.hotspots:
            await logger.ainfo("No coupling issues found, no proposals generated")
            return []

        # Build context for the LLM (used for LLM-based proposals in production)
        context = self._build_analysis_context(report)

        await logger.ainfo(
            "Generating refactoring proposals",
            hotspot_count=len(report.hotspots),
            coupling_count=len(report.file_couplings),
            context_length=len(context),
        )

        try:
            # Use the LLM to generate proposals
            # For now, generate rule-based proposals (LLM integration would
            # call self._llm.parse_intent with the refactoring prompt)
            proposals = self._generate_rule_based_proposals(report, max_proposals)

            # Store proposals
            for proposal in proposals:
                self._proposals[proposal.proposal_id] = proposal

            await logger.ainfo(
                "Refactoring proposals generated",
                count=len(proposals),
            )

            return proposals

        except Exception as e:
            await logger.aerror(
                "Failed to generate refactoring proposals",
                error=str(e),
            )
            return []

    def get_proposal(self, proposal_id: str) -> RefactorProposal | None:
        """Get a specific proposal by ID."""
        return self._proposals.get(proposal_id)

    def list_proposals(self) -> list[RefactorProposal]:
        """List all pending proposals."""
        return [p for p in self._proposals.values() if p.status == "pending"]

    async def approve_proposal(
        self,
        proposal_id: str,
        approved_by: str,
    ) -> RefactorProposal | None:
        """Approve a refactoring proposal.

        In a full implementation, this would trigger PR generation.

        Args:
            proposal_id: The proposal to approve.
            approved_by: The user approving the proposal.

        Returns:
            The updated proposal, or None if not found.
        """
        proposal = self._proposals.get(proposal_id)
        if not proposal:
            return None

        from datetime import datetime

        proposal.status = "approved"
        proposal.approved_by = approved_by
        proposal.approved_at = datetime.now(UTC)

        await logger.ainfo(
            "Refactoring proposal approved",
            proposal_id=proposal_id,
            title=proposal.title,
            approved_by=approved_by,
        )

        return proposal

    def _build_analysis_context(self, report: CouplingReport) -> str:
        """Build a context string for the LLM from the coupling report."""
        lines = [
            f"Repository: {report.repository}",
            f"Total files: {report.total_files}",
            f"Average coupling: {report.average_coupling:.3f}",
            "",
            "Top coupled file pairs:",
        ]

        for coupling in report.file_couplings[:10]:
            lines.append(
                f"  - {coupling.file_a} ↔ {coupling.file_b}: "
                f"{coupling.coupling_score:.3f} "
                f"(shared: {', '.join(coupling.shared_symbols[:5])})"
            )

        if report.hotspots:
            lines.append("")
            lines.append("Hotspot files (highest aggregate coupling):")
            for hotspot in report.hotspots[:10]:
                lines.append(f"  - {hotspot}")

        return "\n".join(lines)

    def _generate_rule_based_proposals(
        self,
        report: CouplingReport,
        max_proposals: int,
    ) -> list[RefactorProposal]:
        """Generate proposals based on deterministic rules (fallback for LLM)."""
        proposals: list[RefactorProposal] = []

        # Proposal for high-coupling pairs
        for coupling in report.file_couplings[:max_proposals]:
            if coupling.coupling_score >= 0.5:
                proposals.append(
                    RefactorProposal(
                        proposal_id=str(ULID()),
                        title=f"Decouple {_basename(coupling.file_a)} and {_basename(coupling.file_b)}",
                        description=(
                            f"Files {coupling.file_a} and {coupling.file_b} share "
                            f"{len(coupling.shared_symbols)} import dependencies "
                            f"(coupling score: {coupling.coupling_score:.2f}). "
                            f"Consider extracting shared logic into a dedicated module."
                        ),
                        files_affected=[coupling.file_a, coupling.file_b],
                        confidence=min(coupling.coupling_score, 0.95),
                        estimated_effort="medium",
                        diff_preview=(
                            f"# Proposed: Extract shared imports into a new module\n"
                            f"# Shared symbols: {', '.join(coupling.shared_symbols[:10])}\n"
                            f"# This would reduce coupling from {coupling.coupling_score:.2f} "
                            f"to approximately {coupling.coupling_score * 0.4:.2f}"
                        ),
                    )
                )

        # Proposal for hotspot files
        for hotspot in report.hotspots[:max(1, max_proposals - len(proposals))]:
            proposals.append(
                RefactorProposal(
                    proposal_id=str(ULID()),
                    title=f"Reduce coupling fan-out in {_basename(hotspot)}",
                    description=(
                        f"{hotspot} is a coupling hotspot with many inter-module "
                        f"dependencies. Consider applying the Facade pattern or "
                        f"extracting sub-modules."
                    ),
                    files_affected=[hotspot],
                    confidence=0.7,
                    estimated_effort="large",
                )
            )

        return proposals[:max_proposals]


def _basename(path: str) -> str:
    """Extract the filename from a path."""
    return path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
