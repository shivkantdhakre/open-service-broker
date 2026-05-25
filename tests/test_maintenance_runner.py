"""
Tests for the AI CI/CD Maintenance Agent.

Verifies:
- Static analysis coupling metrics computation.
- Architectural drift detection (god modules, coupling fan-out, circular dependencies).
- MaintenanceRunner orchestration and markdown report/PR body generation.
- GitHubAdapter in both dry-run and non-dry-run modes.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from broker.config import Settings
from broker.schemas.maintenance import (
    CouplingReport,
    DriftAlert,
    DriftSeverity,
    DriftType,
    RefactorProposal,
)
from broker.services.code_analyzer import CodeAnalyzer
from broker.services.github_integration import GitHubAdapter, GitProviderAdapter
from broker.services.maintenance_runner import MaintenanceRunner


class MockGitProvider(GitProviderAdapter):
    """Mock Git provider to verify MaintenanceRunner interactions."""

    def __init__(self) -> None:
        self.created_branches: list[str] = []
        self.committed_changes: list[dict[str, Any]] = []
        self.created_prs: list[dict[str, str]] = []

    async def create_branch(self, branch_name: str) -> None:
        self.created_branches.append(branch_name)

    async def commit_changes(
        self,
        branch_name: str,
        commit_message: str,
        file_changes: dict[str, str],
    ) -> None:
        self.committed_changes.append({
            "branch_name": branch_name,
            "commit_message": commit_message,
            "file_changes": file_changes,
        })

    async def create_pull_request(
        self,
        branch_name: str,
        title: str,
        body: str,
    ) -> str:
        self.created_prs.append({
            "branch_name": branch_name,
            "title": title,
            "body": body,
        })
        return f"https://github.com/mock-repo/pull/{len(self.created_prs)}"


@pytest.fixture
def mock_repo(tmp_path: Path) -> Path:
    """Create a temporary Python project structure to run static analysis on."""
    # module_a: imports module_b, module_c, and system sys
    module_a = tmp_path / "module_a.py"
    module_a.write_text("import module_b\nimport module_c\nimport sys\n", encoding="utf-8")

    # module_b: imports module_c and module_a (circular dependency with module_a)
    module_b = tmp_path / "module_b.py"
    module_b.write_text("import module_c\nimport module_a\n", encoding="utf-8")

    # module_c: no imports
    module_c = tmp_path / "module_c.py"
    module_c.write_text("def hello():\n    print('hello')\n", encoding="utf-8")

    # god_module: >500 lines to trigger GOD_MODULE alert
    god_module = tmp_path / "god_module.py"
    lines = ["x = 1" for _ in range(505)]
    god_module.write_text("\n".join(lines), encoding="utf-8")

    # high_fanout: imports 21 different modules to trigger COUPLING_INCREASE alert
    high_fanout = tmp_path / "high_fanout.py"
    fanout_lines = [f"import m{i}" for i in range(21)]
    high_fanout.write_text("\n".join(fanout_lines), encoding="utf-8")

    return tmp_path


def test_analyzer_coupling_metrics(mock_repo: Path) -> None:
    """Verify CodeAnalyzer calculates correct coupling metrics on mock repository."""
    analyzer = CodeAnalyzer()
    report = analyzer.analyze_coupling(str(mock_repo))

    assert isinstance(report, CouplingReport)
    assert report.repository == str(mock_repo)
    assert report.total_files == 5
    assert report.total_modules == 5
    assert isinstance(report.average_coupling, float)
    assert len(report.hotspots) > 0
    assert len(report.recommendations) > 0


def test_analyzer_detect_drift(mock_repo: Path) -> None:
    """Verify CodeAnalyzer flags architectural drift correctly."""
    analyzer = CodeAnalyzer()
    # Build graph first by running coupling analysis
    analyzer.analyze_coupling(str(mock_repo))

    alerts = analyzer.detect_drift(str(mock_repo))

    assert len(alerts) >= 3

    drift_types = {a.drift_type for a in alerts}
    assert DriftType.GOD_MODULE in drift_types
    assert DriftType.COUPLING_INCREASE in drift_types
    assert DriftType.CIRCULAR_DEPENDENCY in drift_types

    # God module validation
    god_alert = next(a for a in alerts if a.drift_type == DriftType.GOD_MODULE)
    assert "god_module.py" in god_alert.component
    assert god_alert.severity == DriftSeverity.MEDIUM
    assert "505 lines" in god_alert.description

    # Coupling increase (high fan-out) validation
    fanout_alert = next(a for a in alerts if a.drift_type == DriftType.COUPLING_INCREASE)
    assert "high_fanout.py" in fanout_alert.component
    assert fanout_alert.severity == DriftSeverity.MEDIUM
    assert "21 modules" in fanout_alert.description

    # Circular dependency validation
    circular_alert = next(a for a in alerts if a.drift_type == DriftType.CIRCULAR_DEPENDENCY)
    assert "module_a" in circular_alert.component
    assert "module_b" in circular_alert.component
    assert circular_alert.severity == DriftSeverity.HIGH


@pytest.mark.asyncio
async def test_maintenance_runner_orchestration(mock_repo: Path) -> None:
    """Verify MaintenanceRunner orchestrates report building and Git operations."""
    settings = Settings()
    git_provider = MockGitProvider()

    runner = MaintenanceRunner(settings, git_provider=git_provider)
    result = await runner.run(str(mock_repo), dry_run=False, create_pr=True)

    assert result["coupling_report"] is not None
    assert len(result["drift_alerts"]) >= 3
    assert result["severity_level"] == 2  # CRITICAL/HIGH severity alerts exist
    assert result["report_md"] is not None
    assert result["pr_url"] == "https://github.com/mock-repo/pull/1"

    # Verify Git branch was created
    assert len(git_provider.created_branches) == 1
    assert git_provider.created_branches[0].startswith("refactor/coupling-mitigation-")

    # Verify markdown report was committed
    assert len(git_provider.committed_changes) == 1
    commit = git_provider.committed_changes[0]
    assert commit["commit_message"] == "docs: add automated coupling and drift report"
    assert "reports/coupling_report.md" in commit["file_changes"]
    assert "🔧 Automated Codebase Coupling & Drift Report" in commit["file_changes"]["reports/coupling_report.md"]

    # Verify PR was opened with summary body
    assert len(git_provider.created_prs) == 1
    pr = git_provider.created_prs[0]
    assert pr["title"].startswith("🔧 AI Maintenance: Coupling Mitigation")
    assert "Key Coupling Metrics" in pr["body"]
    assert "Architectural Alerts summary" in pr["body"]


@pytest.mark.asyncio
async def test_github_adapter_dry_run() -> None:
    """Verify GitHubAdapter does not perform actual network calls in dry_run mode."""
    adapter = GitHubAdapter(repo_name="owner/repo", token="dummy-token", dry_run=True)
    assert adapter.dry_run is True

    # These should complete without any actual network/GitHub requests
    await adapter.create_branch("dummy-branch")
    await adapter.commit_changes("dummy-branch", "dummy-msg", {"file.py": "content"})
    pr_url = await adapter.create_pull_request("dummy-branch", "PR Title", "PR Body")

    assert pr_url == "https://github.com/dry-run/repo/pull/mock"


@pytest.mark.asyncio
async def test_github_adapter_validation() -> None:
    """Verify GitHubAdapter raises errors when initialized incorrectly in non-dry-run mode."""
    with pytest.raises(ValueError, match="GITHUB_REPO env var or repo_name must be set"):
        GitHubAdapter(repo_name="", token="some-token", dry_run=False)

    with pytest.raises(ValueError, match="GITHUB_TOKEN env var or token must be set"):
        GitHubAdapter(repo_name="owner/repo", token="", dry_run=False)


@pytest.mark.asyncio
async def test_github_adapter_live_client_interactions() -> None:
    """Verify GitHubAdapter interacts with PyGithub library when dry_run is disabled."""
    mock_github_class = MagicMock()
    mock_repo = MagicMock()
    mock_branch = MagicMock()
    mock_contents = MagicMock()
    mock_pr = MagicMock()

    mock_github_class.return_value.get_repo.return_value = mock_repo
    mock_repo.default_branch = "main"
    mock_repo.get_branch.return_value = mock_branch
    mock_branch.commit.sha = "abcdef123456"

    mock_pr.number = 99
    mock_pr.html_url = "https://github.com/owner/repo/pull/99"
    mock_repo.create_pull.return_value = mock_pr

    # Configure mock contents for file update/create
    mock_contents.content = base64.b64encode(b"old content")
    mock_contents.sha = "file-sha-123"
    mock_repo.get_contents.side_effect = [mock_contents, Exception("Not Found")]

    mock_github_module = MagicMock()
    mock_github_module.Github = mock_github_class

    with patch.dict("sys.modules", {"github": mock_github_module}):
        adapter = GitHubAdapter(repo_name="owner/repo", token="secret-token", dry_run=False)

        # 1. Test create branch
        await adapter.create_branch("feature/test-branch")
        mock_github_class.return_value.get_repo.assert_called_with("owner/repo")
        mock_repo.get_branch.assert_called_with("main")
        mock_repo.create_git_ref.assert_called_with(ref="refs/heads/feature/test-branch", sha="abcdef123456")

        # 2. Test commit changes (both update existing and create new file)
        file_changes = {
            "existing_file.py": "new content",
            "new_file.py": "brand new content",
        }
        await adapter.commit_changes("feature/test-branch", "Commit files", file_changes)
        
        # update_file verification
        mock_repo.update_file.assert_called_with(
            path="existing_file.py",
            message="Commit files (update existing_file.py)",
            content="new content",
            sha="file-sha-123",
            branch="feature/test-branch",
        )

        # create_file verification
        mock_repo.create_file.assert_called_with(
            path="new_file.py",
            message="Commit files (create new_file.py)",
            content="brand new content",
            branch="feature/test-branch",
        )

        # 3. Test create pull request
        pr_url = await adapter.create_pull_request("feature/test-branch", "Mitigate Coupling", "Body text")
        mock_repo.create_pull.assert_called_with(
            title="Mitigate Coupling",
            body="Body text",
            base="main",
            head="feature/test-branch",
        )
        assert pr_url == "https://github.com/owner/repo/pull/99"


def test_approve_proposal_route():
    """Verify POST /api/v1/maintenance/proposals/{id}/approve enqueues SQS task."""
    from fastapi.testclient import TestClient
    from broker.main import app
    from broker.dependencies import get_sqs_service
    from broker.services.refactoring_agent import RefactoringAgent
    from broker.schemas.maintenance import RefactorProposal
    import broker.routers.maintenance as maint_router

    # Create mock SQS service
    mock_sqs = MagicMock()
    mock_sqs.enqueue_task = AsyncMock(return_value="msg-123")

    # Set up a mock RefactoringAgent with an approved proposal mock
    mock_proposal = RefactorProposal(
        proposal_id="proposal-123",
        title="Decouple modules",
        description="Refactor code",
        files_affected=["a.py"],
        diff_preview="--- diff",
        confidence=0.9,
        estimated_effort="small",
        status="approved",
    )
    
    mock_agent = MagicMock(spec=RefactoringAgent)
    mock_agent.approve_proposal = AsyncMock(return_value=mock_proposal)
    maint_router._refactoring_agent = mock_agent

    # Override dependencies
    app.dependency_overrides[get_sqs_service] = lambda: mock_sqs

    client = TestClient(app)
    
    response = client.post("/api/v1/maintenance/proposals/proposal-123/approve")
    
    assert response.status_code == 200
    res_data = response.json()
    assert res_data["proposal_id"] == "proposal-123"
    assert res_data["status"] == "approved"

    # Verify SQS task was enqueued
    mock_sqs.enqueue_task.assert_called_once()
    called_task = mock_sqs.enqueue_task.call_args[0][0]
    assert called_task.task_type == "maintenance"
    assert called_task.resource_id == "proposal-123"
    assert called_task.configuration["proposal"]["proposal_id"] == "proposal-123"
    
    # Clean up overrides
    app.dependency_overrides.clear()
    maint_router._refactoring_agent = None
