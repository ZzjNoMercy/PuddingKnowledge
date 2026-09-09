from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from knowledge_platform.baseline.legacy_runtime_registry import (
    load_legacy_runtime_probe_registry,
    validate_legacy_runtime_probe_registry,
)


def test_checked_in_legacy_registry_validates_as_partial_and_has_all_capabilities() -> None:
    root = Path(__file__).resolve().parents[2]
    document, validation = load_legacy_runtime_probe_registry(
        root / "docs/knowledge-platform/phase-0a-legacy-runtime-call-probes.yaml",
        repo_root=root,
    )

    assert document["status"] == "local-observer-coverage-captured-boundary-review-pending"
    assert validation.reviewed is False
    assert len(validation.executed_probe_ids) == 9
    assert len(validation.pending_boundary_review_ids) == 9


def test_legacy_registry_require_reviewed_is_fail_closed() -> None:
    root = Path(__file__).resolve().parents[2]
    document, _ = load_legacy_runtime_probe_registry(
        root / "docs/knowledge-platform/phase-0a-legacy-runtime-call-probes.yaml",
        repo_root=root,
    )

    with pytest.raises(ValueError, match="pending boundary reviews"):
        validate_legacy_runtime_probe_registry(document, repo_root=root, require_reviewed=True)


def test_legacy_registry_strict_review_requires_current_replay_evidence() -> None:
    root = Path(__file__).resolve().parents[2]
    document, _ = load_legacy_runtime_probe_registry(
        root / "docs/knowledge-platform/phase-0a-legacy-runtime-call-probes.yaml",
        repo_root=root,
    )
    for probe in document["executed_probes"]:
        probe["boundary_review"]["status"] = "reviewed"

    with pytest.raises(ValueError, match="requires replay evidence"):
        validate_legacy_runtime_probe_registry(document, repo_root=root, require_reviewed=True)

    with pytest.raises(ValueError, match="every probe to be replayed"):
        validate_legacy_runtime_probe_registry(
            document,
            repo_root=root,
            require_reviewed=True,
            replay_evidence={
                "status": "matched",
                "replayed_probe_ids": [],
                "mismatched_probe_ids": [],
                "skipped_probe_ids": [],
            },
        )


def test_checked_in_legacy_registry_replays_all_observers() -> None:
    result = subprocess.run(
        [
            "backend/.venv/bin/python",
            "backend/scripts/phase0a_legacy_runtime_probe_registry.py",
            "--replay",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)

    assert report["replay"]["status"] == "matched"
    assert report["replay"]["mismatched_probe_ids"] == []
    assert report["replay"]["skipped_probe_ids"] == []
    assert report["replay"]["replayed_probe_ids"] == report["executed_probe_ids"]
    assert set(report["replay"]["run_counts"].values()) == {2}


def test_connector_observer_restores_knowledge_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from scripts import phase0a_connectors_capture_observer as observer

    original = str(tmp_path / "caller-knowledge")
    monkeypatch.setenv("PUDDINGCLAW_KNOWLEDGE_DIR", original)
    observer.observe()
    assert observer.os.environ["PUDDINGCLAW_KNOWLEDGE_DIR"] == original


def test_legacy_registry_rejects_missing_output(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    document, _ = load_legacy_runtime_probe_registry(
        root / "docs/knowledge-platform/phase-0a-legacy-runtime-call-probes.yaml",
        repo_root=root,
    )
    document["executed_probes"][0]["output"]["path"] = "docs/knowledge-platform/does-not-exist.json"

    with pytest.raises(ValueError, match="missing"):
        validate_legacy_runtime_probe_registry(document, repo_root=root)


def test_legacy_registry_rejects_missing_output_without_repo_root() -> None:
    root = Path(__file__).resolve().parents[2]
    document, _ = load_legacy_runtime_probe_registry(
        root / "docs/knowledge-platform/phase-0a-legacy-runtime-call-probes.yaml",
    )
    document["executed_probes"][0].pop("output")

    with pytest.raises(ValueError, match="output must be a mapping"):
        validate_legacy_runtime_probe_registry(document)
