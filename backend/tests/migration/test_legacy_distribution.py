"""Distribution marker checks against the original mixed source tree."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.migration


def _legacy_root() -> Path:
    value = os.environ.get("PUDDINGKNOWLEDGE_LEGACY_SOURCE", "").strip()
    if not value:
        raise RuntimeError("PUDDINGKNOWLEDGE_LEGACY_SOURCE is required for source distribution checks")
    return Path(value).expanduser().resolve()


def test_phase9_boundary_shadow_runs_against_original_repository(tmp_path: Path) -> None:
    from scripts.phase9_local_distribution_boundary_shadow import run_shadow

    result = run_shadow(repo_root=_legacy_root(), output_dir=tmp_path)
    assert result["status"] == "PHASE9_DISTRIBUTION_BOUNDARY_INVENTORY_PASS_NOT_ACTIVATABLE"
    assert result["activation_allowed"] is False
    assert result["files_moved"] is False
    assert result["files_deleted"] is False
    assert result["missing_anchors"] == []
    assert result["coverage"]["unclassified_count"] == 0
    assert result["coverage"]["ambiguous_count"] == 0
    assert result["console_surface"]["status"] == "verified"
    assert result["console_surface"]["surface_ids"] == [
        "knowledge",
        "analytics",
        "oauth",
        "imports",
        "sources",
        "schema",
        "results",
        "notifications",
    ]
    assert result["console_surface"]["file_count"] == 5


def test_phase9_mixed_surface_marker_shadow_is_manual_review_only(tmp_path: Path) -> None:
    from scripts.phase9_local_mixed_surface_shadow import run_shadow

    result = run_shadow(repo_root=_legacy_root(), output_path=tmp_path / "mixed.json")
    assert result["status"] == "PHASE9_MIXED_SURFACE_MARKER_PASS_NOT_ACTIVATABLE"
    assert result["activation_allowed"] is False
    assert result["execution_allowed"] is False
    assert result["manual_review_required"] is True
    assert result["semantic_equivalence_proven"] is False
    assert len(result["probes"]) == 7
    assert all(item["status"] == "verified" for item in result["probes"])
    assert all(item["ownership_matches"] is True for item in result["probes"])
    assert all(all(lines for lines in item["required_marker_lines"]) for item in result["probes"])
    assert all(all(len(lines) <= 8 for lines in item["required_marker_lines"]) for item in result["probes"])
    assert all(len(item["required_marker_line_counts"]) == item["required_marker_count"] for item in result["probes"])
    assert all(len(item["symbol_evidence"]) == item["required_marker_count"] for item in result["probes"])
    assert all(
        evidence["action"] in {"retain_harness", "extract_platform", "shared_adapter"}
        for item in result["probes"]
        for evidence in item["symbol_evidence"]
    )


def test_phase9_mixed_surface_schema_rejects_invalid_action_missing_evidence_and_long_samples(tmp_path: Path) -> None:
    from jsonschema import Draft202012Validator

    from scripts.phase9_local_mixed_surface_shadow import _SCHEMA, run_shadow

    schema = json.loads(_SCHEMA.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    source = run_shadow(repo_root=_legacy_root(), output_path=tmp_path / "source.json")
    invalid_action = copy.deepcopy(source)
    invalid_action["probes"][0]["symbol_evidence"][0]["action"] = "invented_action"
    assert list(validator.iter_errors(invalid_action))

    missing_evidence = copy.deepcopy(source)
    del missing_evidence["probes"][0]["symbol_evidence"]
    assert list(validator.iter_errors(missing_evidence))

    long_samples = copy.deepcopy(source)
    long_samples["probes"][0]["required_marker_lines"][0] = list(range(1, 10))
    assert list(validator.iter_errors(long_samples))
