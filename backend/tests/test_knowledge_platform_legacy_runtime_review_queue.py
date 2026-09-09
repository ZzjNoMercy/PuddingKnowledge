from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from knowledge_platform.baseline.legacy_runtime_review_queue import (
    LegacyRuntimeReviewQueueError,
    load_legacy_runtime_review_queue,
)
from scripts.phase0a_legacy_runtime_review_queue import build_review_queue


def _replay_evidence() -> dict[str, object]:
    probe_ids = [
        "catalog_migration_rehearsal",
        "connectors_and_capture",
        "database_nl2sql_and_readonly_execute",
        "document_rag",
        "lease_and_jobs",
        "package_export",
        "semantic_assets_and_authoring",
        "table_query",
        "wiki_query_and_compile",
    ]
    return {
        "status": "matched",
        "replayed_probe_ids": probe_ids,
        "run_counts": {probe_id: 2 for probe_id in probe_ids},
    }


def test_real_legacy_runtime_review_queue_is_path_free_and_approval_only(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    output = tmp_path / "queue.json"
    report = build_review_queue(
        registry_path=root / "docs/knowledge-platform/phase-0a-legacy-runtime-call-probes.yaml",
        output_path=output,
        repo_root=root,
        _replay_evidence=_replay_evidence(),
    )
    assert report["summary"] == {
        "all_replayed": True,
        "approval_required": True,
        "pending_review_count": 9,
        "replayed_probe_count": 9,
        "required_callee_complete": True,
        "review_item_count": 9,
    }
    text = output.read_text(encoding="utf-8")
    assert "/Users/" not in text
    assert "/private/" not in text
    assert all(item["review_status"] == "pending" for item in report["items"])
    assert all(item["review_id"].startswith("sha256:") for item in report["items"])
    load_legacy_runtime_review_queue(json.loads(text))


def test_review_queue_rejects_tampered_review_id(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    report = build_review_queue(
        registry_path=root / "docs/knowledge-platform/phase-0a-legacy-runtime-call-probes.yaml",
        output_path=tmp_path / "queue.json",
        repo_root=root,
        _replay_evidence=_replay_evidence(),
    )
    tampered = copy.deepcopy(report)
    tampered["items"][0]["reason"] = "changed"
    with pytest.raises(LegacyRuntimeReviewQueueError, match="review_id"):
        load_legacy_runtime_review_queue(tampered)


def test_review_queue_rejects_non_portable_reason(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    report = build_review_queue(
        registry_path=root / "docs/knowledge-platform/phase-0a-legacy-runtime-call-probes.yaml",
        output_path=tmp_path / "queue.json",
        repo_root=root,
        _replay_evidence=_replay_evidence(),
    )
    tampered = copy.deepcopy(report)
    tampered["items"][0]["reason"] = "leak /Users/pet/Documents/knowledge"
    with pytest.raises(LegacyRuntimeReviewQueueError, match="portable"):
        load_legacy_runtime_review_queue(tampered)


def test_review_queue_builder_rejects_symlink_output(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    target = tmp_path / "target.json"
    output = tmp_path / "queue.json"
    output.symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        build_review_queue(
            registry_path=root / "docs/knowledge-platform/phase-0a-legacy-runtime-call-probes.yaml",
            output_path=output,
            repo_root=root,
            _replay_evidence=_replay_evidence(),
        )
