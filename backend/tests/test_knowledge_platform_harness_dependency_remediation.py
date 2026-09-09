from __future__ import annotations

import json
from pathlib import Path

import pytest

from knowledge_platform.distribution.harness_dependency_remediation import (
    HarnessDependencyRemediationError,
    build_harness_dependency_remediation_plan,
)
from knowledge_platform.distribution.harness_dependency_scan import (
    DependencyFinding,
    HarnessDependencyScanResult,
)


def _scan() -> HarnessDependencyScanResult:
    return HarnessDependencyScanResult(
        findings=(
            DependencyFinding("backend/a.py", 8, "legacy_knowledge_import"),
            DependencyFinding("backend/a.py", 20, "virtual_knowledge_path"),
            DependencyFinding("backend/b.py", 4, "platform_python_import"),
        ),
        scanned_file_count=2,
        missing_file_paths=(),
        manual_review_pending=2,
    )


def test_remediation_plan_groups_findings_without_source_text() -> None:
    result = build_harness_dependency_remediation_plan(_scan()).to_dict()

    assert result["status"] == "PHASE10_HARNESS_DEPENDENCY_REMEDIATION_REQUIRED"
    assert result["activation_allowed"] is False
    assert result["rc_proof"] is False
    assert result["finding_count"] == 3
    assert result["files"][0]["path"] == "backend/a.py"
    assert result["rules"][0]["rule_id"] == "legacy_knowledge_import"
    assert all("source" not in json.dumps(item) for item in result["files"])


def test_actual_remediation_report_is_path_and_secret_free() -> None:
    from jsonschema import validate

    report = Path("artifacts/phase0b-local-catalog/phase10-harness-dependency-remediation.json")
    if not report.exists():
        pytest.skip("local remediation report has not been generated")
    data = json.loads(report.read_text(encoding="utf-8"))
    schema = Path("docs/knowledge-platform/harness-dependency-remediation.schema.json")
    validate(data, json.loads(schema.read_text(encoding="utf-8")))
    assert data["status"] == "PHASE10_HARNESS_DEPENDENCY_REMEDIATION_REQUIRED"
    assert data["finding_count"] == 100
    assert len(data["files"]) == 10
    serialized = json.dumps(data, ensure_ascii=False)
    assert "/Users/" not in serialized
    assert "file://" not in serialized
    assert "secret" not in serialized.lower()


def test_remediation_requires_blocked_scan_with_findings() -> None:
    scan = HarnessDependencyScanResult(findings=(), scanned_file_count=0, missing_file_paths=(), manual_review_pending=0)
    with pytest.raises(HarnessDependencyRemediationError):
        build_harness_dependency_remediation_plan(scan)
