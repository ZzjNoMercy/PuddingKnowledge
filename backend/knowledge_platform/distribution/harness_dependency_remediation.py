"""Turn Harness dependency findings into a path-free remediation checklist.

The checklist is planning evidence only.  It does not edit mixed files, create
repositories, or claim that the future Harness is independent.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any

from .harness_dependency_scan import DependencyFinding, HarnessDependencyScanResult

_FORMAT = "agent-knowledge-platform-phase10-harness-dependency-remediation/v1"

_RULE_ACTIONS = {
    "legacy_knowledge_import": (
        "remove_or_adapter",
        "move Knowledge implementation to Platform or replace it with an external capability adapter",
    ),
    "legacy_analytics_import": (
        "remove_or_adapter",
        "move Analytics implementation to Platform or replace it with an external capability adapter",
    ),
    "legacy_vanna_import": (
        "platform_boundary",
        "keep Vanna ownership in the local Platform Collection and remove it from the pure Harness target",
    ),
    "platform_python_import": (
        "wire_contract",
        "replace same-checkout Python imports with versioned REST/MCP/Workspace contracts",
    ),
    "legacy_knowledge_tool_name": (
        "protocol_cleanup",
        "remove legacy Knowledge tool aliases from the Harness protocol and use capability/resource identifiers",
    ),
    "virtual_knowledge_path": (
        "route_cleanup",
        "remove embedded Knowledge routes from Harness and resolve external Resource URI/configuration",
    ),
}


class HarnessDependencyRemediationError(ValueError):
    """Raised when remediation evidence is unsafe or internally inconsistent."""


@dataclass(frozen=True, slots=True)
class RemediationRule:
    rule_id: str
    action: str
    rationale: str
    finding_count: int

    def __post_init__(self) -> None:
        if self.rule_id not in _RULE_ACTIONS:
            raise HarnessDependencyRemediationError("unknown remediation rule")
        if self.action != _RULE_ACTIONS[self.rule_id][0] or self.rationale != _RULE_ACTIONS[self.rule_id][1]:
            raise HarnessDependencyRemediationError("remediation rule does not match its contract")
        if not isinstance(self.finding_count, int) or self.finding_count < 1:
            raise HarnessDependencyRemediationError("remediation finding count is invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "action": self.action,
            "rationale": self.rationale,
            "finding_count": self.finding_count,
        }


@dataclass(frozen=True, slots=True)
class RemediationFile:
    path: str
    finding_count: int
    rule_ids: tuple[str, ...]
    lines_by_rule: tuple[tuple[str, tuple[int, ...]], ...]

    def __post_init__(self) -> None:
        if not self.path or self.path.startswith(("/", "~")) or ".." in self.path.split("/"):
            raise HarnessDependencyRemediationError("remediation path must be relative")
        if not isinstance(self.finding_count, int) or self.finding_count < 1:
            raise HarnessDependencyRemediationError("remediation file count is invalid")
        if tuple(sorted(set(self.rule_ids))) != self.rule_ids:
            raise HarnessDependencyRemediationError("remediation rule IDs must be sorted and unique")
        if self.finding_count != sum(len(lines) for _rule_id, lines in self.lines_by_rule):
            raise HarnessDependencyRemediationError("remediation file count does not match line evidence")
        for rule_id, lines in self.lines_by_rule:
            if rule_id not in self.rule_ids or rule_id not in _RULE_ACTIONS:
                raise HarnessDependencyRemediationError("remediation file has unknown rule")
            if tuple(sorted(set(lines))) != lines or any(line < 1 for line in lines):
                raise HarnessDependencyRemediationError("remediation line evidence is invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "finding_count": self.finding_count,
            "rule_ids": list(self.rule_ids),
            "lines_by_rule": [{"rule_id": rule_id, "lines": list(lines)} for rule_id, lines in self.lines_by_rule],
        }


@dataclass(frozen=True, slots=True)
class HarnessDependencyRemediationPlan:
    scan_status: str
    finding_count: int
    manual_review_pending: int
    rules: tuple[RemediationRule, ...]
    files: tuple[RemediationFile, ...]

    def __post_init__(self) -> None:
        if self.scan_status != "PHASE10_HARNESS_DEPENDENCY_SCAN_BLOCKED":
            raise HarnessDependencyRemediationError("remediation requires a blocked dependency scan")
        if self.finding_count < 1 or self.manual_review_pending < 1:
            raise HarnessDependencyRemediationError("remediation requires pending findings and review")
        if self.finding_count != sum(rule.finding_count for rule in self.rules):
            raise HarnessDependencyRemediationError("rule counts do not match total findings")
        if self.finding_count != sum(item.finding_count for item in self.files):
            raise HarnessDependencyRemediationError("file counts do not match total findings")

    @property
    def status(self) -> str:
        return "PHASE10_HARNESS_DEPENDENCY_REMEDIATION_REQUIRED"

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": _FORMAT,
            "status": self.status,
            "activation_allowed": False,
            "rc_proof": False,
            "scan_status": self.scan_status,
            "finding_count": self.finding_count,
            "manual_review_pending": self.manual_review_pending,
            "rules": [rule.to_dict() for rule in self.rules],
            "files": [item.to_dict() for item in self.files],
            "scope": "path-free remediation planning evidence; no source text, file edits, repository extraction, or release proof",
        }


def build_harness_dependency_remediation_plan(
    scan: HarnessDependencyScanResult,
) -> HarnessDependencyRemediationPlan:
    if not scan.findings:
        raise HarnessDependencyRemediationError("cannot build remediation plan without findings")
    by_rule = Counter(finding.rule_id for finding in scan.findings)
    rules = tuple(
        RemediationRule(rule_id, _RULE_ACTIONS[rule_id][0], _RULE_ACTIONS[rule_id][1], by_rule[rule_id])
        for rule_id in sorted(by_rule)
    )
    by_file: dict[str, list[DependencyFinding]] = defaultdict(list)
    for finding in scan.findings:
        by_file[finding.path].append(finding)
    files: list[RemediationFile] = []
    for path in sorted(by_file):
        findings = sorted(by_file[path], key=lambda item: (item.rule_id, item.line))
        lines_by_rule = tuple(
            (rule_id, tuple(item.line for item in findings if item.rule_id == rule_id))
            for rule_id in sorted({item.rule_id for item in findings})
        )
        files.append(
            RemediationFile(
                path=path,
                finding_count=len(findings),
                rule_ids=tuple(rule_id for rule_id, _lines in lines_by_rule),
                lines_by_rule=lines_by_rule,
            )
        )
    return HarnessDependencyRemediationPlan(
        scan_status=scan.status,
        finding_count=len(scan.findings),
        manual_review_pending=scan.manual_review_pending,
        rules=rules,
        files=tuple(files),
    )


__all__ = [
    "HarnessDependencyRemediationError",
    "HarnessDependencyRemediationPlan",
    "RemediationFile",
    "RemediationRule",
    "build_harness_dependency_remediation_plan",
]
