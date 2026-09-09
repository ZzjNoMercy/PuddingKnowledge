from __future__ import annotations

import copy
import hashlib
import json

import pytest

from knowledge_platform.semantic.crosswalk_collision_decision import (
    CrosswalkCollisionDecisionError,
    load_crosswalk_collision_decision,
    prepare_crosswalk_collision_decision,
    resolve_crosswalk_collision_policy,
)
from knowledge_platform.semantic.crosswalk_collision_review import load_crosswalk_collision_review_queue
from knowledge_platform.semantic.vehicle_series import find_vehicle_series_canonical_collisions


def _queue() -> dict:
    """Build a small deterministic queue instead of reading a source artifact.

    These tests exercise the target decision protocol.  The real Phase 7
    shadow report is migration evidence and is intentionally not a fixture of
    the extracted repository.
    """

    canonical_rows = [
        {"brand": f"Brand {index}", "serial_name": f"Series {index}"}
        for index in range(7)
        for _ in (0, 1)
    ]
    for index in range(7):
        canonical_rows[index * 2 + 1]["serial_name"] = f"Series{index}"
    collisions = find_vehicle_series_canonical_collisions(canonical_rows)
    queue_items = []
    for collision in collisions:
        item = {
            "candidate_count": 2,
            "candidate_digests": sorted(collision["candidate_digests"]),
            "decision_status": "pending",
            "normalized_key_digest": "sha256:" + hashlib.sha256(
                str(collision["normalized_key"]).encode("utf-8")
            ).hexdigest(),
        }
        item["review_id"] = "sha256:" + hashlib.sha256(
            json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        queue_items.append(item)
    queue = {
        "format": "agent-knowledge-platform-crosswalk-collision-review-queue/v1",
        "status": "PHASE7_CROSSWALK_COLLISION_REVIEW_REQUIRED_NOT_ACTIVATABLE",
        "activation": "not-activated",
        "execution_allowed": False,
        "policy": "test",
        "canonical_source": {
            "content_digest": "sha256:" + "1" * 64,
            "identity_basis": ["brand", "serial_name"],
            "stable_identity_columns": [],
        },
        "summary": {"approval_required": True, "collision_count": len(queue_items), "pending_review_count": len(queue_items)},
        "items": queue_items,
    }
    load_crosswalk_collision_review_queue(queue)
    return queue


def test_prepare_crosswalk_collision_decision_requires_all_explicit_candidates() -> None:
    queue = _queue()
    selections = [
        {"review_id": item["review_id"], "candidate_digest": item["candidate_digests"][0]}
        for item in queue["items"]
    ]
    candidate = prepare_crosswalk_collision_decision(queue, selections)
    assert candidate["status"] == "PHASE7_CROSSWALK_COLLISION_DECISION_CANDIDATE_READY_NOT_ACTIVATABLE"
    assert candidate["activation"] == "not-activated"
    assert candidate["execution_allowed"] is False
    assert candidate["summary"] == {"collision_count": 7, "reviewed_count": 7}
    assert len(candidate["decisions"]) == 7
    load_crosswalk_collision_decision(candidate)


def test_prepare_rejects_missing_unknown_or_unlisted_selection() -> None:
    queue = _queue()
    selections = [
        {"review_id": item["review_id"], "candidate_digest": item["candidate_digests"][0]}
        for item in queue["items"][:-1]
    ]
    with pytest.raises(CrosswalkCollisionDecisionError, match="every pending"):
        prepare_crosswalk_collision_decision(queue, selections)
    selections.append({"review_id": "sha256:" + "0" * 64, "candidate_digest": queue["items"][-1]["candidate_digests"][0]})
    with pytest.raises(CrosswalkCollisionDecisionError, match="unknown"):
        prepare_crosswalk_collision_decision(queue, selections)
    selections[-1] = {
        "review_id": queue["items"][-1]["review_id"],
        "candidate_digest": "sha256:" + "0" * 64,
    }
    with pytest.raises(CrosswalkCollisionDecisionError, match="not in"):
        prepare_crosswalk_collision_decision(queue, selections)


def test_decision_candidate_count_is_data_driven() -> None:
    queue = _queue()
    reduced = copy.deepcopy(queue)
    reduced["items"] = reduced["items"][:1]
    reduced["summary"] = {"approval_required": True, "collision_count": 1, "pending_review_count": 1}
    decision = prepare_crosswalk_collision_decision(
        reduced,
        [{"review_id": reduced["items"][0]["review_id"], "candidate_digest": reduced["items"][0]["candidate_digests"][0]}],
    )
    assert decision["summary"] == {"collision_count": 1, "reviewed_count": 1}
    load_crosswalk_collision_decision(decision)


def test_load_rejects_tampered_decision_candidate() -> None:
    queue = _queue()
    selections = [
        {"review_id": item["review_id"], "candidate_digest": item["candidate_digests"][0]}
        for item in queue["items"]
    ]
    candidate = prepare_crosswalk_collision_decision(queue, selections)
    tampered = copy.deepcopy(candidate)
    tampered["decisions"][0]["candidate_digest"] = "sha256:" + "0" * 64
    with pytest.raises(CrosswalkCollisionDecisionError):
        load_crosswalk_collision_decision(tampered)


def test_resolve_policy_recaptures_current_collision_set() -> None:
    canonical_rows = [
        {"brand": f"Brand {index}", "serial_name": f"Series {index}"}
        for index in range(7)
        for _ in (0, 1)
    ]
    for index in range(7):
        canonical_rows[index * 2 + 1]["serial_name"] = f"Series{index}"
    collisions = find_vehicle_series_canonical_collisions(canonical_rows)
    queue_items = []
    for collision in collisions:
        item = {
            "candidate_count": 2,
            "candidate_digests": sorted(collision["candidate_digests"]),
            "decision_status": "pending",
            "normalized_key_digest": "sha256:" + hashlib.sha256(
                str(collision["normalized_key"]).encode("utf-8")
            ).hexdigest(),
        }
        item["review_id"] = "sha256:" + hashlib.sha256(
            json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        queue_items.append(item)
    queue = {
        "format": "agent-knowledge-platform-crosswalk-collision-review-queue/v1",
        "status": "PHASE7_CROSSWALK_COLLISION_REVIEW_REQUIRED_NOT_ACTIVATABLE",
        "activation": "not-activated",
        "execution_allowed": False,
        "policy": "test",
        "canonical_source": {
            "content_digest": "sha256:" + "1" * 64,
            "identity_basis": ["brand", "serial_name"],
            "stable_identity_columns": [],
        },
        "summary": {"approval_required": True, "collision_count": 7, "pending_review_count": 7},
        "items": queue_items,
    }
    load_crosswalk_collision_review_queue(queue)
    decisions = [
        {"review_id": item["review_id"], "candidate_digest": item["candidate_digests"][0]}
        for item in queue_items
    ]
    decision = prepare_crosswalk_collision_decision(queue, decisions)
    policy = resolve_crosswalk_collision_policy(
        queue=queue, decision_candidate=decision, canonical_rows=canonical_rows
    )
    assert len(policy) == 7


def test_resolve_policy_rejects_canonical_collision_drift() -> None:
    queue = _queue()
    selections = [
        {"review_id": item["review_id"], "candidate_digest": item["candidate_digests"][0]}
        for item in queue["items"]
    ]
    decision = prepare_crosswalk_collision_decision(queue, selections)
    with pytest.raises(CrosswalkCollisionDecisionError, match="collision set"):
        resolve_crosswalk_collision_policy(queue=queue, decision_candidate=decision, canonical_rows=[])
