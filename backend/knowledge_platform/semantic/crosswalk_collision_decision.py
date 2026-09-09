"""Prepare explicit, non-activating decisions for local Crosswalk collisions."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from .crosswalk_collision_review import load_crosswalk_collision_review_queue
from .vehicle_series import find_vehicle_series_canonical_collisions

_FORMAT = "agent-knowledge-platform-crosswalk-collision-decision-candidate/v1"
_STATUS = "PHASE7_CROSSWALK_COLLISION_DECISION_CANDIDATE_READY_NOT_ACTIVATABLE"
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class CrosswalkCollisionDecisionError(ValueError):
    """Raised when explicit collision decisions are incomplete or unsafe."""


def _digest(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise CrosswalkCollisionDecisionError(f"{label} is invalid")
    return value


def _document_digest(document: Mapping[str, object]) -> str:
    encoded = (json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _decisions_digest(decisions: Sequence[Mapping[str, str]]) -> str:
    encoded = json.dumps(list(decisions), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _normalized_key_digest(value: object) -> str:
    return f"sha256:{hashlib.sha256(str(value).encode('utf-8')).hexdigest()}"


def prepare_crosswalk_collision_decision(
    queue: Mapping[str, object], selections: Sequence[Mapping[str, str]]
) -> dict[str, Any]:
    """Validate a complete explicit selection set and return a non-executable candidate."""

    normalized_queue = load_crosswalk_collision_review_queue(queue)
    queue_items = normalized_queue["items"]
    if not isinstance(queue_items, list):  # pragma: no cover - guarded by queue validator
        raise CrosswalkCollisionDecisionError("Crosswalk queue items are invalid")
    if len(selections) != len(queue_items):
        raise CrosswalkCollisionDecisionError("every pending Crosswalk review must have one explicit selection")

    by_review_id = {str(item["review_id"]): item for item in queue_items}
    decisions: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, selection in enumerate(selections):
        if not isinstance(selection, Mapping) or set(selection) != {"review_id", "candidate_digest"}:
            raise CrosswalkCollisionDecisionError(f"selection[{index}] fields are invalid")
        review_id = _digest(selection["review_id"], label=f"selection[{index}] review ID")
        candidate_digest = _digest(selection["candidate_digest"], label=f"selection[{index}] candidate digest")
        if review_id in seen:
            raise CrosswalkCollisionDecisionError("duplicate Crosswalk review ID selection")
        item = by_review_id.get(review_id)
        if item is None:
            raise CrosswalkCollisionDecisionError("selection contains an unknown Crosswalk review ID")
        candidates = item["candidate_digests"]
        if not isinstance(candidates, list) or candidate_digest not in candidates:
            raise CrosswalkCollisionDecisionError("selected candidate is not in the reviewed collision evidence")
        seen.add(review_id)
        decisions.append(
            {
                "review_id": review_id,
                "candidate_digest": candidate_digest,
                "decision_status": "reviewed",
            }
        )
    if seen != set(by_review_id):
        raise CrosswalkCollisionDecisionError("selection set does not cover the current Crosswalk queue")
    decisions.sort(key=lambda item: item["review_id"])
    candidate: dict[str, Any] = {
        "format": _FORMAT,
        "status": _STATUS,
        "activation": "not-activated",
        "execution_allowed": False,
        "approval_required": True,
        "policy": "Explicit local review selections only; a separate execution step must re-capture canonical data and revalidate this candidate.",
        "queue": {
            "content_digest": _document_digest(normalized_queue),
            "format": normalized_queue["format"],
            "canonical_source": normalized_queue["canonical_source"],
        },
        "summary": {"collision_count": len(decisions), "reviewed_count": len(decisions)},
        "decisions_digest": _decisions_digest(decisions),
        "decisions": decisions,
    }
    return candidate


def resolve_crosswalk_collision_policy(
    *,
    queue: Mapping[str, object],
    decision_candidate: Mapping[str, object],
    canonical_rows: Sequence[Mapping[str, object]],
) -> dict[str, str]:
    """Resolve a reviewed candidate into raw normalized keys only after current-data recapture."""

    normalized_queue = load_crosswalk_collision_review_queue(queue)
    normalized_decision = load_crosswalk_collision_decision(decision_candidate)
    decision_queue = normalized_decision["queue"]
    if not isinstance(decision_queue, Mapping):  # pragma: no cover - guarded by decision validator
        raise CrosswalkCollisionDecisionError("Crosswalk decision queue binding is invalid")
    if decision_queue["content_digest"] != _document_digest(normalized_queue):
        raise CrosswalkCollisionDecisionError("Crosswalk decision candidate is bound to a stale queue")
    if decision_queue["canonical_source"] != normalized_queue["canonical_source"]:
        raise CrosswalkCollisionDecisionError("Crosswalk decision canonical source binding is stale")

    collisions = find_vehicle_series_canonical_collisions(canonical_rows)
    by_key_digest = {_normalized_key_digest(item["normalized_key"]): item for item in collisions}
    queue_items = normalized_queue["items"]
    decisions = normalized_decision["decisions"]
    if not isinstance(queue_items, list) or not isinstance(decisions, list):  # pragma: no cover
        raise CrosswalkCollisionDecisionError("Crosswalk collision evidence is invalid")
    queue_by_review_id = {str(item["review_id"]): item for item in queue_items}
    if set(by_key_digest) != {str(item["normalized_key_digest"]) for item in queue_items}:
        raise CrosswalkCollisionDecisionError("current canonical collision set does not match the reviewed queue")
    policy: dict[str, str] = {}
    for decision in decisions:
        review_id = str(decision["review_id"])
        item = queue_by_review_id.get(review_id)
        if item is None:
            raise CrosswalkCollisionDecisionError("decision refers to a review no longer present in the queue")
        key_digest = str(item["normalized_key_digest"])
        collision = by_key_digest.get(key_digest)
        if collision is None or list(item["candidate_digests"]) != sorted(str(value) for value in collision["candidate_digests"]):
            raise CrosswalkCollisionDecisionError("current canonical candidate evidence does not match the queue")
        selected = str(decision["candidate_digest"])
        if selected not in item["candidate_digests"]:
            raise CrosswalkCollisionDecisionError("decision selects a candidate outside the current evidence")
        policy[str(collision["normalized_key"])] = selected
    if set(policy) != {str(item["normalized_key"]) for item in collisions}:
        raise CrosswalkCollisionDecisionError("decision candidate does not cover the current collision set")
    return policy


def load_crosswalk_collision_decision(document: object) -> dict[str, Any]:
    if not isinstance(document, Mapping):
        raise CrosswalkCollisionDecisionError("Crosswalk decision candidate must be an object")
    required = {
        "activation",
        "approval_required",
        "decisions",
        "decisions_digest",
        "execution_allowed",
        "format",
        "policy",
        "queue",
        "status",
        "summary",
    }
    if set(document) != required or document["format"] != _FORMAT or document["status"] != _STATUS:
        raise CrosswalkCollisionDecisionError("Crosswalk decision candidate envelope is invalid")
    if document["activation"] != "not-activated" or document["approval_required"] is not True or document["execution_allowed"] is not False:
        raise CrosswalkCollisionDecisionError("Crosswalk decision candidate activation policy is invalid")
    queue = document["queue"]
    if not isinstance(queue, Mapping) or set(queue) != {"canonical_source", "content_digest", "format"}:
        raise CrosswalkCollisionDecisionError("Crosswalk decision queue binding is invalid")
    _digest(queue["content_digest"], label="Crosswalk decision queue digest")
    if queue["format"] != "agent-knowledge-platform-crosswalk-collision-review-queue/v1":
        raise CrosswalkCollisionDecisionError("Crosswalk decision queue format is invalid")
    source = queue["canonical_source"]
    if not isinstance(source, Mapping) or set(source) != {"content_digest", "identity_basis", "stable_identity_columns"}:
        raise CrosswalkCollisionDecisionError("Crosswalk decision canonical source binding is invalid")
    _digest(source["content_digest"], label="Crosswalk decision canonical source digest")
    for key in ("identity_basis", "stable_identity_columns"):
        if not isinstance(source[key], list) or any(not isinstance(value, str) or not value.strip() for value in source[key]):
            raise CrosswalkCollisionDecisionError(f"Crosswalk decision canonical source {key} is invalid")
    decisions = document["decisions"]
    if not isinstance(decisions, list) or not decisions:
        raise CrosswalkCollisionDecisionError("Crosswalk decision candidate must contain decisions")
    seen: set[str] = set()
    for index, decision in enumerate(decisions):
        if not isinstance(decision, Mapping) or set(decision) != {"candidate_digest", "decision_status", "review_id"}:
            raise CrosswalkCollisionDecisionError(f"decision[{index}] fields are invalid")
        review_id = _digest(decision["review_id"], label=f"decision[{index}] review ID")
        _digest(decision["candidate_digest"], label=f"decision[{index}] candidate digest")
        if decision["decision_status"] != "reviewed" or review_id in seen:
            raise CrosswalkCollisionDecisionError("Crosswalk decisions must be unique and reviewed")
        seen.add(review_id)
    if _decisions_digest(decisions) != _digest(document["decisions_digest"], label="Crosswalk decisions digest"):
        raise CrosswalkCollisionDecisionError("Crosswalk decision list digest does not match evidence")
    summary = document["summary"]
    if not isinstance(summary, Mapping) or set(summary) != {"collision_count", "reviewed_count"}:
        raise CrosswalkCollisionDecisionError("Crosswalk decision summary is invalid")
    if (
        not isinstance(summary["collision_count"], int)
        or summary["collision_count"] < 1
        or summary["reviewed_count"] != summary["collision_count"]
        or summary["collision_count"] != len(decisions)
    ):
        raise CrosswalkCollisionDecisionError("Crosswalk decision count does not match decisions")
    return dict(document)


__all__ = [
    "CrosswalkCollisionDecisionError",
    "load_crosswalk_collision_decision",
    "prepare_crosswalk_collision_decision",
    "resolve_crosswalk_collision_policy",
]
