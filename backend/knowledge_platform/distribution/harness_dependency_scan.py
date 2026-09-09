"""Static preflight for Knowledge dependencies in the future Harness RC.

This is intentionally not an RC pass: mixed files still need symbol-level
cleanup and the resulting repository does not exist.  The scanner only emits
relative paths, line numbers, and stable rule IDs, never source text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .extraction import ExtractionPathPlan

_FORMAT = "agent-knowledge-platform-phase10-harness-dependency-scan/v1"
_PATTERNS = (
    ("legacy_knowledge_import", re.compile(r"^\s*(?:from|import)\s+knowledge(?:\.|\s|$)")),
    ("legacy_analytics_import", re.compile(r"^\s*(?:from|import)\s+analytics(?:\.|\s|$)")),
    ("legacy_vanna_import", re.compile(r"^\s*(?:from|import)\s+vanna(?:\.|\s|$)")),
    ("platform_python_import", re.compile(r"knowledge_platform|knowledge_contracts")),
    ("legacy_knowledge_tool_name", re.compile(r"llamaindex_knowledge_query")),
    ("virtual_knowledge_path", re.compile(r"(?<![A-Za-z0-9_])/knowledge(?:/|\b)")),
)


class HarnessDependencyScanError(ValueError):
    """Raised when the scan input or output contract is unsafe."""


@dataclass(frozen=True, slots=True)
class DependencyFinding:
    path: str
    line: int
    rule_id: str

    def __post_init__(self) -> None:
        if not self.path or self.path.startswith(("/", "~")) or ".." in Path(self.path).parts:
            raise HarnessDependencyScanError("dependency finding path must be relative")
        if not isinstance(self.line, int) or self.line < 1:
            raise HarnessDependencyScanError("dependency finding line is invalid")
        if not any(rule_id == self.rule_id for rule_id, _pattern in _PATTERNS):
            raise HarnessDependencyScanError("dependency finding rule is unknown")

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "line": self.line, "rule_id": self.rule_id}


@dataclass(frozen=True, slots=True)
class HarnessDependencyScanResult:
    findings: tuple[DependencyFinding, ...]
    scanned_file_count: int
    missing_file_paths: tuple[str, ...]
    manual_review_pending: int

    def __post_init__(self) -> None:
        if not isinstance(self.scanned_file_count, int) or self.scanned_file_count < 0:
            raise HarnessDependencyScanError("scanned file count is invalid")
        if not isinstance(self.manual_review_pending, int) or self.manual_review_pending < 0:
            raise HarnessDependencyScanError("manual review count is invalid")
        if any(path.startswith(("/", "~")) or ".." in Path(path).parts for path in self.missing_file_paths):
            raise HarnessDependencyScanError("missing path is not relative")

    @property
    def status(self) -> str:
        return "PHASE10_HARNESS_DEPENDENCY_SCAN_BLOCKED"

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": _FORMAT,
            "status": self.status,
            "activation_allowed": False,
            "rc_proof": False,
            "scanned_file_count": self.scanned_file_count,
            "missing_file_paths": list(self.missing_file_paths),
            "manual_review_pending": self.manual_review_pending,
            "finding_count": len(self.findings),
            "findings": [finding.to_dict() for finding in self.findings],
            "scope": "same-checkout mixed-file preflight; not a pure Harness RC dependency proof",
        }


def scan_harness_dependency_shadow(
    *, repo_root: Path, mixed_file_plans: tuple[ExtractionPathPlan, ...]
) -> HarnessDependencyScanResult:
    repo_root = repo_root.expanduser().resolve()
    findings: list[DependencyFinding] = []
    missing: list[str] = []
    scanned = 0
    for plan in mixed_file_plans:
        path = plan.path
        source = repo_root / path
        if not source.is_file():
            missing.append(path)
            continue
        scanned += 1
        try:
            lines = source.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as error:
            raise HarnessDependencyScanError("dependency scan source is unreadable") from error
        for line_number, line in enumerate(lines, start=1):
            for rule_id, pattern in _PATTERNS:
                if pattern.search(line):
                    findings.append(DependencyFinding(path, line_number, rule_id))
    return HarnessDependencyScanResult(
        findings=tuple(findings),
        scanned_file_count=scanned,
        missing_file_paths=tuple(sorted(missing)),
        manual_review_pending=len(mixed_file_plans),
    )


__all__ = [
    "DependencyFinding",
    "HarnessDependencyScanError",
    "HarnessDependencyScanResult",
    "scan_harness_dependency_shadow",
]
