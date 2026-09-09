from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from knowledge_platform.semantic.crosswalk_collision_review import (
    CrosswalkCollisionReviewError,
    load_crosswalk_collision_review_queue,
)
from scripts.phase7_crosswalk_collision_review_queue import build_review_queue


def test_real_crosswalk_collision_queue_is_path_free_and_pending(tmp_path: Path) -> None:
    report = build_review_queue(
        report_path=_report_path(tmp_path),
        output_path=tmp_path / "queue.json",
    )
    text = (tmp_path / "queue.json").read_text(encoding="utf-8")
    assert len(report["items"]) == 7
    assert "/Users/" not in text
    assert "/private/" not in text
    assert all(item["decision_status"] == "pending" for item in report["items"])
    load_crosswalk_collision_review_queue(json.loads(text))


def test_crosswalk_collision_queue_rejects_tampered_review_id(tmp_path: Path) -> None:
    report = build_review_queue(
        report_path=_report_path(tmp_path),
        output_path=tmp_path / "queue.json",
    )
    tampered = copy.deepcopy(report)
    tampered["items"][0]["candidate_digests"][0] = "sha256:" + "0" * 64
    with pytest.raises(CrosswalkCollisionReviewError, match="review ID"):
        load_crosswalk_collision_review_queue(tampered)


def test_crosswalk_collision_queue_count_is_data_driven(tmp_path: Path) -> None:
    report = _queue_report(tmp_path)
    reduced = copy.deepcopy(report)
    reduced["items"] = reduced["items"][:1]
    reduced["summary"] = {"approval_required": True, "collision_count": 1, "pending_review_count": 1}
    normalized = load_crosswalk_collision_review_queue(reduced)
    assert normalized["summary"] == {"approval_required": True, "collision_count": 1, "pending_review_count": 1}


def _queue_report(tmp_path: Path) -> dict:
    return build_review_queue(
        report_path=_report_path(tmp_path),
        output_path=tmp_path / "queue.json",
    )


def _report_path(tmp_path: Path) -> Path:
    """Write the smallest valid Phase 7 shadow report for queue tests."""

    summaries = [
        {
            "candidate_count": 2,
            "candidate_digests": [
                "sha256:" + f"{index + 1:064x}",
                "sha256:" + f"{index + 101:064x}",
            ],
            "normalized_key_digest": "sha256:" + f"{index + 201:064x}",
        }
        for index in range(7)
    ]
    path = tmp_path / "crosswalk-shadow-report.json"
    path.write_text(
        json.dumps(
            {
                "status": "PHASE7_CROSSWALK_REAL_CANONICAL_SHADOW_BLOCKED_DATA_QUALITY",
                "canonical_collision_summaries": summaries,
                "canonical_source": {
                    "identity_basis": ["brand", "serial_name"],
                    "stable_identity_columns": [],
                },
                "source_snapshot": {"content_digest": "sha256:" + "1" * 64},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path
