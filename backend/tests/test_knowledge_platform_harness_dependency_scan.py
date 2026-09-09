from __future__ import annotations

from pathlib import Path

import pytest

from knowledge_platform.distribution import (
    DependencyFinding,
    ExtractionPathPlan,
    HarnessDependencyScanError,
    scan_harness_dependency_shadow,
)


def _plan(path: str) -> ExtractionPathPlan:
    return ExtractionPathPlan(
        path,
        "manual",
        "shared-review",
        "split symbols",
        ("Harness behavior",),
        ("Platform business behavior",),
    )


def test_dependency_scan_detects_legacy_and_platform_coupling_without_source_text(tmp_path: Path) -> None:
    source = tmp_path / "mixed.py"
    source.write_text(
        "from knowledge.query import query\n"
        "from analytics.nl2sql import train\n"
        "from vanna.base import Vanna\n"
        "tool = 'llamaindex_knowledge_query'\n"
        "root = '/knowledge/assets'\n",
        encoding="utf-8",
    )
    result = scan_harness_dependency_shadow(repo_root=tmp_path, mixed_file_plans=(_plan("mixed.py"),))
    assert result.status == "PHASE10_HARNESS_DEPENDENCY_SCAN_BLOCKED"
    assert result.scanned_file_count == 1
    assert result.manual_review_pending == 1
    assert result.to_dict()["rc_proof"] is False
    assert {finding.rule_id for finding in result.findings} == {
        "legacy_knowledge_import",
        "legacy_analytics_import",
        "legacy_vanna_import",
        "legacy_knowledge_tool_name",
        "virtual_knowledge_path",
    }
    assert all("knowledge.query" not in str(finding.to_dict()) for finding in result.findings)


def test_dependency_scan_records_missing_relative_paths_and_current_mixed_files_are_not_clean() -> None:
    from knowledge_platform.distribution import build_phase10_extraction_manifest

    manifest = build_phase10_extraction_manifest(repo_root=Path(__file__).resolve().parents[2])
    result = scan_harness_dependency_shadow(
        repo_root=Path(__file__).resolve().parents[2], mixed_file_plans=manifest.mixed_file_plans
    )
    assert result.manual_review_pending == 23
    assert result.findings
    assert result.status == "PHASE10_HARNESS_DEPENDENCY_SCAN_BLOCKED"


def test_dependency_finding_rejects_unsafe_path_and_rule() -> None:
    with pytest.raises(HarnessDependencyScanError):
        DependencyFinding("/Users/pet/source.py", 1, "legacy_knowledge_import")
    with pytest.raises(HarnessDependencyScanError):
        DependencyFinding("source.py", 1, "unknown")
