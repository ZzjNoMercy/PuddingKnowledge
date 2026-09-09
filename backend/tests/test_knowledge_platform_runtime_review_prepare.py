from __future__ import annotations

from pathlib import Path

import pytest

from knowledge_platform.baseline import load_runtime_probe_registry
from scripts.phase0a_runtime_review_prepare import prepare_reviewed_registry
from scripts.phase0a_runtime_review_queue import build_review_queue
from tests.test_knowledge_platform_runtime_review_queue import _replay_evidence


def _paths(root: Path, tmp_path: Path) -> tuple[Path, Path, dict[str, object]]:
    queue_path = tmp_path / "queue.json"
    queue = build_review_queue(
        registry_path=root / "docs/knowledge-platform/phase-0a-runtime-call-probes.yaml",
        output_path=queue_path,
        repo_root=root,
        _replay_evidence=_replay_evidence(),
    )
    return root / "docs/knowledge-platform/phase-0a-runtime-call-probes.yaml", queue_path, queue


def test_prepare_requires_all_explicit_review_ids_and_writes_candidate_only(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    registry_path, queue_path, queue = _paths(root, tmp_path)
    ids = [str(item["review_id"]) for item in queue["items"]]
    with pytest.raises(ValueError, match="every pending"):
        prepare_reviewed_registry(
            registry_path=registry_path,
            queue_path=queue_path,
            output_path=tmp_path / "candidate.yaml",
            repo_root=root,
            approved_review_ids=ids[:-1],
            _replay_evidence={**_replay_evidence(), "mismatched_probe_ids": [], "skipped_probe_ids": []},
        )
    result = prepare_reviewed_registry(
        registry_path=registry_path,
        queue_path=queue_path,
        output_path=tmp_path / "candidate.yaml",
        repo_root=root,
        approved_review_ids=ids,
        _replay_evidence={**_replay_evidence(), "mismatched_probe_ids": [], "skipped_probe_ids": []},
    )
    assert result["reviewed"] is True
    candidate = tmp_path / "candidate.yaml"
    document, validation = load_runtime_probe_registry(candidate, repo_root=root, require_complete=False)
    assert document["status"] == "complete"
    assert validation.complete is True
    assert all(probe["boundary_review"]["status"] == "reviewed" for probe in document["executed_probes"])
    assert all(probe["boundary_review"]["approval_source"] == "explicit-local-review-queue" for probe in document["executed_probes"])
    assert registry_path.read_text(encoding="utf-8") != candidate.read_text(encoding="utf-8")


def test_prepare_rejects_incomplete_replay_evidence(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    registry_path, queue_path, queue = _paths(root, tmp_path)
    ids = [str(item["review_id"]) for item in queue["items"]]
    replay = {**_replay_evidence(), "mismatched_probe_ids": ["catalog_processing"], "skipped_probe_ids": []}
    with pytest.raises(ValueError, match="complete match"):
        prepare_reviewed_registry(
            registry_path=registry_path,
            queue_path=queue_path,
            output_path=tmp_path / "candidate.yaml",
            repo_root=root,
            approved_review_ids=ids,
            _replay_evidence=replay,
        )


def test_prepare_rejects_symlink_output(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    registry_path, queue_path, queue = _paths(root, tmp_path)
    output = tmp_path / "candidate.yaml"
    output.symlink_to(tmp_path / "target.yaml")
    with pytest.raises(ValueError, match="symlink"):
        prepare_reviewed_registry(
            registry_path=registry_path,
            queue_path=queue_path,
            output_path=output,
            repo_root=root,
            approved_review_ids=[str(item["review_id"]) for item in queue["items"]],
            _replay_evidence={**_replay_evidence(), "mismatched_probe_ids": [], "skipped_probe_ids": []},
        )
