"""
Code Analyzer — static analysis for coupling detection and architectural drift.

Parses Python source files using AST to compute coupling metrics,
detect tightly coupled modules, and flag architectural drift.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path
from typing import Any

import structlog
from ulid import ULID

from broker.schemas.maintenance import (
    CouplingReport,
    DriftAlert,
    DriftSeverity,
    DriftType,
    FileCoupling,
)

logger = structlog.get_logger()


class CodeAnalyzer:
    """Static code analyzer for coupling detection and drift monitoring."""

    def __init__(self) -> None:
        self._import_graph: dict[str, set[str]] = defaultdict(set)

    def analyze_coupling(
        self,
        repo_path: str,
        include_patterns: list[str] | None = None,
        exclude_patterns: list[str] | None = None,
    ) -> CouplingReport:
        """Analyze coupling between modules in a Python codebase.

        Parses all Python files to build an import graph and compute
        afferent/efferent coupling metrics.

        Args:
            repo_path: Path to the repository root.
            include_patterns: Glob patterns for files to include.
            exclude_patterns: Glob patterns for files to exclude.

        Returns:
            CouplingReport with detailed analysis.
        """
        include = include_patterns or ["*.py"]
        exclude = exclude_patterns or ["**/test_*", "**/__pycache__/*", "**/venv/*"]

        repo = Path(repo_path)
        python_files = self._find_files(repo, include, exclude)

        if not python_files:
            return CouplingReport(
                repository=repo_path,
                total_files=0,
                recommendations=["No Python files found in the specified path."],
            )

        # Build import graph
        self._import_graph.clear()
        module_sizes: dict[str, int] = {}

        for file_path in python_files:
            rel_path = str(file_path.relative_to(repo))
            imports = self._extract_imports(file_path)
            self._import_graph[rel_path] = imports
            module_sizes[rel_path] = self._count_lines(file_path)

        # Compute coupling scores
        couplings = self._compute_coupling_scores(python_files, repo)

        # Identify hotspots
        hotspots = self._identify_hotspots(couplings)

        # Generate recommendations
        recommendations = self._generate_recommendations(couplings, module_sizes)

        avg_coupling = (
            sum(c.coupling_score for c in couplings) / len(couplings)
            if couplings else 0.0
        )

        return CouplingReport(
            repository=repo_path,
            total_files=len(python_files),
            total_modules=len(set(self._import_graph.keys())),
            file_couplings=couplings[:50],  # Top 50 most coupled pairs
            average_coupling=round(avg_coupling, 3),
            hotspots=hotspots,
            recommendations=recommendations,
        )

    def detect_drift(
        self,
        repo_path: str,
        baseline: dict[str, Any] | None = None,
    ) -> list[DriftAlert]:
        """Detect architectural drift in the codebase.

        Args:
            repo_path: Path to the repository root.
            baseline: Optional baseline metrics for comparison.

        Returns:
            List of DriftAlert instances.
        """
        alerts: list[DriftAlert] = []
        repo = Path(repo_path)

        # Check for god modules (files with too many imports or too many lines)
        for file_path, imports in self._import_graph.items():
            full_path = repo / file_path
            if not full_path.exists():
                continue

            line_count = self._count_lines(full_path)

            # God module detection: >500 lines or >20 imports
            if line_count > 500:
                alerts.append(
                    DriftAlert(
                        alert_id=str(ULID()),
                        component=file_path,
                        drift_type=DriftType.GOD_MODULE,
                        severity=DriftSeverity.HIGH if line_count > 1000 else DriftSeverity.MEDIUM,
                        description=f"{file_path} has {line_count} lines — consider splitting.",
                        suggested_action="Break this module into smaller, focused modules.",
                    )
                )

            if len(imports) > 20:
                alerts.append(
                    DriftAlert(
                        alert_id=str(ULID()),
                        component=file_path,
                        drift_type=DriftType.COUPLING_INCREASE,
                        severity=DriftSeverity.MEDIUM,
                        description=f"{file_path} imports {len(imports)} modules — high fan-out.",
                        suggested_action="Reduce dependencies by introducing abstractions.",
                    )
                )

        # Circular dependency detection
        circular = self._detect_circular_dependencies()
        for cycle in circular:
            alerts.append(
                DriftAlert(
                    alert_id=str(ULID()),
                    component=" → ".join(cycle),
                    drift_type=DriftType.CIRCULAR_DEPENDENCY,
                    severity=DriftSeverity.HIGH,
                    description=(
                        f"Circular dependency detected: {' → '.join(cycle)}"
                    ),
                    suggested_action=(
                        "Break the cycle by extracting shared logic "
                        "into a new module."
                    ),
                )
            )

        return alerts

    def _find_files(
        self,
        repo: Path,
        include: list[str],
        exclude: list[str],
    ) -> list[Path]:
        """Find files matching include patterns, excluding exclude patterns."""
        files: list[Path] = []

        for pattern in include:
            for file_path in repo.rglob(pattern):
                if file_path.is_file():
                    excluded = any(
                        file_path.match(ep) for ep in exclude
                    )
                    if not excluded:
                        files.append(file_path)

        return sorted(set(files))

    def _extract_imports(self, file_path: Path) -> set[str]:
        """Extract imported module names from a Python file using AST."""
        imports: set[str] = set()

        try:
            source = file_path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(file_path))

            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        imports.add(alias.name.split(".")[0])
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imports.add(node.module.split(".")[0])

        except (SyntaxError, UnicodeDecodeError):
            pass

        return imports

    def _count_lines(self, file_path: Path) -> int:
        """Count non-empty, non-comment lines in a file."""
        try:
            lines = file_path.read_text(encoding="utf-8").splitlines()
            return sum(
                1 for line in lines
                if line.strip() and not line.strip().startswith("#")
            )
        except (UnicodeDecodeError, OSError):
            return 0

    def _compute_coupling_scores(
        self,
        files: list[Path],
        repo: Path,
    ) -> list[FileCoupling]:
        """Compute coupling scores between file pairs based on shared imports."""
        couplings: list[FileCoupling] = []
        file_imports: dict[str, set[str]] = {}

        for f in files:
            rel = str(f.relative_to(repo))
            file_imports[rel] = self._import_graph.get(rel, set())

        keys = list(file_imports.keys())
        for i in range(len(keys)):
            for j in range(i + 1, len(keys)):
                a, b = keys[i], keys[j]
                shared = file_imports[a] & file_imports[b]
                if shared:
                    total = file_imports[a] | file_imports[b]
                    score = len(shared) / max(len(total), 1)

                    if score > 0.1:  # Only report significant coupling
                        couplings.append(
                            FileCoupling(
                                file_a=a,
                                file_b=b,
                                coupling_score=round(score, 3),
                                shared_symbols=sorted(shared),
                            )
                        )

        # Sort by coupling score descending
        couplings.sort(key=lambda c: c.coupling_score, reverse=True)
        return couplings

    def _identify_hotspots(self, couplings: list[FileCoupling]) -> list[str]:
        """Identify files that appear most frequently in high-coupling pairs."""
        file_scores: dict[str, float] = defaultdict(float)

        for coupling in couplings:
            file_scores[coupling.file_a] += coupling.coupling_score
            file_scores[coupling.file_b] += coupling.coupling_score

        # Top 10% of files by aggregate coupling score
        sorted_files = sorted(file_scores.items(), key=lambda x: x[1], reverse=True)
        cutoff = max(1, len(sorted_files) // 10)
        return [f[0] for f in sorted_files[:cutoff]]

    def _detect_circular_dependencies(self) -> list[list[str]]:
        """Detect circular dependencies in the import graph."""
        cycles: list[list[str]] = []
        visited: set[str] = set()
        rec_stack: set[str] = set()

        # Build a resolved dependency graph: file path -> set of file paths
        resolved_graph: dict[str, set[str]] = defaultdict(set)
        for node, imports in self._import_graph.items():
            for imp in imports:
                for key in self._import_graph:
                    parts = Path(key).with_suffix("").parts
                    if imp in parts and key != node:
                        resolved_graph[node].add(key)

        def dfs(node: str, path: list[str]) -> None:
            visited.add(node)
            rec_stack.add(node)
            path.append(node)

            for neighbor in resolved_graph.get(node, set()):
                if neighbor not in visited:
                    dfs(neighbor, path)
                elif neighbor in rec_stack:
                    # Found cycle
                    cycle_start = path.index(neighbor)
                    cycle = [*path[cycle_start:], neighbor]
                    cycles.append(cycle)

            path.pop()
            rec_stack.discard(node)

        for node in self._import_graph:
            if node not in visited:
                dfs(node, [])

        return cycles[:10]  # Limit to 10 cycles

    def _generate_recommendations(
        self,
        couplings: list[FileCoupling],
        module_sizes: dict[str, int],
    ) -> list[str]:
        """Generate actionable recommendations based on analysis results."""
        recommendations: list[str] = []

        # High coupling recommendation
        high_coupling = [c for c in couplings if c.coupling_score > 0.5]
        if high_coupling:
            recommendations.append(
                f"{len(high_coupling)} file pairs have coupling score > 0.5. "
                "Consider extracting shared logic into dedicated modules."
            )

        # Large module recommendation
        large_modules = [m for m, size in module_sizes.items() if size > 300]
        if large_modules:
            recommendations.append(
                f"{len(large_modules)} modules exceed 300 lines. "
                "Consider breaking them into smaller, focused modules."
            )

        if not recommendations:
            recommendations.append("Codebase coupling is within acceptable limits.")

        return recommendations
