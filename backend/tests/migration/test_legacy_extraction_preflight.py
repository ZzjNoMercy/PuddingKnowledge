"""Phase 10 observations against the original mixed source checkout."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.migration


def _legacy_root() -> Path:
    value = os.environ.get("PUDDINGKNOWLEDGE_LEGACY_SOURCE", "").strip()
    if not value:
        raise RuntimeError("PUDDINGKNOWLEDGE_LEGACY_SOURCE is required for extraction observations")
    return Path(value).expanduser().resolve()


def test_phase10_extraction_preflight_observes_mixed_source_anchors() -> None:
    from knowledge_platform.distribution import build_phase10_extraction_manifest

    manifest = build_phase10_extraction_manifest(repo_root=_legacy_root())
    replay = build_phase10_extraction_manifest(repo_root=_legacy_root())
    assert manifest.status == "PHASE10_EXTRACTION_PREFLIGHT_NOT_EXECUTABLE"
    assert manifest.executable is False
    assert manifest.activation_allowed is False
    assert manifest.extraction_method == "git-filter-repo"
    assert manifest.target_repositories == ("puddingknowledge", "puddingharness")
    assert "backend/graph/deepagents_manager.py" in manifest.mixed_file_paths
    assert len(manifest.mixed_file_paths) == 23
    assert len(manifest.mixed_file_plans) == 23
    assert all(plan.action == "manual" and plan.target == "shared-review" for plan in manifest.mixed_file_plans)
    assert all(plan.exclude for plan in manifest.mixed_file_plans)
    router = next(plan for plan in manifest.mixed_file_plans if plan.path.endswith("tool_intent_router.py"))
    assert router.preserve == ()
    assert router.exclude == ("entire ToolIntentRouter middleware",)
    assert not manifest.missing_mixed_file_paths
    assert manifest.to_dict() == replay.to_dict()
    assert len(manifest.canonical_digest()) == 64


def test_phase10_preflight_report_is_path_free_for_mixed_source(tmp_path: Path) -> None:
    from scripts.phase10_extraction_preflight import run_preflight

    path = tmp_path / "preflight.json"
    result = run_preflight(repo_root=_legacy_root(), output_path=path)
    payload = path.read_text(encoding="utf-8")
    assert result["status"] == "PHASE10_EXTRACTION_PREFLIGHT_NOT_EXECUTABLE"
    assert '"missing_mixed_file_paths": []' in payload
    assert '"phase_gate_statuses"' in payload
    assert '"mixed_file_plans"' in payload
    assert '"replay_consistent": true' in payload
    assert "/Users/" not in payload
    assert "file://" not in payload
