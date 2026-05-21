"""
Maintenance Router — API endpoints for CI/CD maintenance agent.

POST /analyze              — Trigger coupling analysis
GET  /drift                — Get architectural drift alerts
GET  /proposals            — List pending refactor proposals
POST /proposals/{id}/approve — Approve a proposal for PR generation
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, HTTPException, status

from broker.dependencies import LLMDep
from broker.schemas.maintenance import (
    AnalyzeRequest,
    CouplingReport,
    DriftAlert,
    RefactorProposal,
)
from broker.services.code_analyzer import CodeAnalyzer
from broker.services.refactoring_agent import RefactoringAgent

logger = structlog.get_logger()

router = APIRouter()

# Module-level instances (would be proper DI in production)
_analyzer = CodeAnalyzer()
_refactoring_agent: RefactoringAgent | None = None
_latest_drift_alerts: list[DriftAlert] = []


def _get_refactoring_agent(llm: LLMDep) -> RefactoringAgent:  # type: ignore[arg-type]
    """Get or create the refactoring agent singleton."""
    global _refactoring_agent
    if _refactoring_agent is None:
        _refactoring_agent = RefactoringAgent(llm)
    return _refactoring_agent


@router.post(
    "/analyze",
    response_model=CouplingReport,
    summary="Trigger coupling analysis",
    description=(
        "Analyzes the specified repository for code coupling, "
        "architectural drift, and generates refactoring proposals."
    ),
)
async def analyze_codebase(
    request: AnalyzeRequest,
    llm: LLMDep,
) -> CouplingReport:
    """Run coupling analysis on a repository."""
    global _latest_drift_alerts

    await logger.ainfo(
        "Starting codebase analysis",
        repository=request.repository_path,
    )

    # Run coupling analysis
    report = _analyzer.analyze_coupling(
        repo_path=request.repository_path,
        include_patterns=request.include_patterns,
        exclude_patterns=request.exclude_patterns,
    )

    # Run drift detection
    drift_alerts = _analyzer.detect_drift(request.repository_path)
    _latest_drift_alerts = drift_alerts

    # Generate refactoring proposals
    agent = _get_refactoring_agent(llm)
    await agent.propose_refactors(report)

    await logger.ainfo(
        "Codebase analysis completed",
        total_files=report.total_files,
        coupling_pairs=len(report.file_couplings),
        drift_alerts=len(drift_alerts),
    )

    return report


@router.get(
    "/drift",
    response_model=list[DriftAlert],
    summary="Get architectural drift alerts",
)
async def get_drift_alerts() -> list[DriftAlert]:
    """Return current architectural drift alerts."""
    return _latest_drift_alerts


@router.get(
    "/proposals",
    response_model=list[RefactorProposal],
    summary="List pending refactor proposals",
)
async def list_proposals(llm: LLMDep) -> list[RefactorProposal]:
    """Return all pending refactoring proposals."""
    agent = _get_refactoring_agent(llm)
    return agent.list_proposals()


@router.post(
    "/proposals/{proposal_id}/approve",
    response_model=RefactorProposal,
    summary="Approve a refactor proposal",
    description=(
        "Approves a refactoring proposal. In a full implementation, "
        "this would trigger automatic PR generation."
    ),
)
async def approve_proposal(
    proposal_id: str,
    llm: LLMDep,
    approved_by: str = "api-user",
) -> RefactorProposal:
    """Approve a refactoring proposal for PR generation."""
    agent = _get_refactoring_agent(llm)
    proposal = await agent.approve_proposal(proposal_id, approved_by)

    if proposal is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "type": "proposal_not_found",
                "title": f"No proposal with ID '{proposal_id}' exists.",
            },
        )

    return proposal
