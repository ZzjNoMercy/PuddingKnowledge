from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from knowledge_platform.baseline.golden_dependency_review import (
    GoldenDependencyReviewError,
    load_golden_dependency_review_queue,
)
from scripts.phase0a_golden_dependency_review_queue import build_review_queue


def test_real_golden_dependency_queue_is_path_free_and_not_frozen(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    report = build_review_queue(
        replay_report_path=root / "artifacts/phase0a/golden-replay-report.json",
        candidate_path=root / "artifacts/phase0a/golden-recapture-candidates/connectors_and_capture.json",
        canonical_path=root / "docs/knowledge-platform/golden-records/connectors_and_capture.json",
        output_path=tmp_path / "queue.json",
    )
    text = (tmp_path / "queue.json").read_text(encoding="utf-8")
    assert "/Users/" not in text
    assert "/private/" not in text
    assert report["summary"] == {"approval_required": True, "pending_review_count": 1, "review_item_count": 1}
    assert report["items"][0]["differing_fields"] == ["evidence"]
    load_golden_dependency_review_queue(json.loads(text))


def test_golden_dependency_queue_rejects_tampered_review_id(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    report = build_review_queue(
        replay_report_path=root / "artifacts/phase0a/golden-replay-report.json",
        candidate_path=root / "artifacts/phase0a/golden-recapture-candidates/connectors_and_capture.json",
        canonical_path=root / "docs/knowledge-platform/golden-records/connectors_and_capture.json",
        output_path=tmp_path / "queue.json",
    )
    tampered = copy.deepcopy(report)
    tampered["items"][0]["options"][0]["description"] = "changed"
    with pytest.raises(GoldenDependencyReviewError, match="review_id"):
        load_golden_dependency_review_queue(tampered)
