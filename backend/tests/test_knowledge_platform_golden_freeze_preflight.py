from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scripts.phase0a_golden_freeze_preflight import build_preflight


def test_real_golden_freeze_preflight_is_path_free_and_blocked_by_dirty_worktree() -> None:
    root = Path(__file__).resolve().parents[2]
    report = build_preflight(
        repo_root=root,
        manifest_path=root / "docs/knowledge-platform/golden-fixture-manifest.yaml",
    )
    assert report["status"] == "PHASE0A_GOLDEN_FREEZE_PREFLIGHT_BLOCKED"
    assert report["activation_allowed"] is False
    assert report["execution_allowed"] is False
    assert report["manifest"]["capability_count"] == 9
    assert report["manifest"]["incomplete_capability_count"] == 0
    assert report["source_snapshot"]["captured"] is True
    assert report["summary"]["freeze_allowed"] is False
    assert "manifest_status_not_frozen" in report["summary"]["blockers"]
    assert "source_snapshot_worktree_dirty" in report["summary"]["blockers"]
    text = json.dumps(report, ensure_ascii=False)
    assert "/Users/" not in text
    assert "/private/" not in text


def test_preflight_rejects_manifest_snapshot_digest_drift(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.yaml"
    source = tmp_path / "source.json"
    source.write_text('{"format":"agent-knowledge-platform-source-snapshot/v1","observation_only":true,"raw_contents_included":false,"repository_revision":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","worktree_clean":true,"capabilities":[],"snapshot_digest":"sha256:' + "0" * 64 + '"}\n', encoding="utf-8")
    manifest.write_text(
        "format: agent-knowledge-platform-golden-fixture-manifest/v1\n"
        "spec_revision: v0.7\n"
        "status: capture-required\n"
        f"source_snapshot:\n  path: {source.name}\n  sha256: {'a' * 64}\n"
        "capabilities:\n  - id: document_rag\n    fixture_files: []\n    baseline: null\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="digest drift"):
        build_preflight(repo_root=tmp_path, manifest_path=manifest)


def test_preflight_reports_uncaptured_source_snapshot_instead_of_crashing(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        "format: agent-knowledge-platform-golden-fixture-manifest/v1\n"
        "spec_revision: v0.7\n"
        "status: capture-required\n"
        "source_snapshot:\n  path: snapshots/source.json\n  sha256: pending\n"
        "capabilities:\n  - id: document_rag\n    fixture_files: []\n    baseline: null\n",
        encoding="utf-8",
    )
    report = build_preflight(repo_root=tmp_path, manifest_path=manifest)
    assert report["source_snapshot"] == {
        "captured": False,
        "declared_sha256": "pending",
        "actual_sha256": "unavailable",
        "repository_revision": "unavailable",
        "worktree_clean": False,
        "raw_contents_included": False,
        "observation_only": True,
    }
    assert "source_snapshot_not_captured" in report["summary"]["blockers"]
