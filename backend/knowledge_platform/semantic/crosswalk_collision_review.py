"""Fail-closed validation for the local Crosswalk collision review queue."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

_FORMAT = "agent-knowledge-platform-crosswalk-collision-review-queue/v1"
_STATUS = "PHASE7_CROSSWALK_COLLISION_REVIEW_REQUIRED_NOT_ACTIVATABLE"
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class CrosswalkCollisionReviewError(ValueError):
    """Raised when a collision queue is unsafe or internally inconsistent."""


def _digest(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise CrosswalkCollisionReviewError(f"{label} is invalid")
    return value


def _review_id(item: Mapping[str, object]) -> str:
    payload = {key: item[key] for key in item if key != "review_id"}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def load_crosswalk_collision_review_queue(document: object) -> dict[str, Any]:
    if not isinstance(document, Mapping):
        raise CrosswalkCollisionReviewError("Crosswalk collision queue must be an object")
    required = {"activation", "canonical_source", "execution_allowed", "format", "items", "policy", "status", "summary"}
    if set(document) != required or document["format"] != _FORMAT or document["status"] != _STATUS:
        raise CrosswalkCollisionReviewError("Crosswalk collision queue envelope is invalid")
    if document["activation"] != "not-activated" or document["execution_allowed"] is not False:
        raise CrosswalkCollisionReviewError("Crosswalk collision queue activation policy is invalid")
    if not isinstance(document["policy"], str) or not document["policy"].strip():
        raise CrosswalkCollisionReviewError("Crosswalk collision queue policy is invalid")
    source = document["canonical_source"]
    if not isinstance(source, Mapping) or set(source) != {"content_digest", "identity_basis", "stable_identity_columns"}:
        raise CrosswalkCollisionReviewError("Crosswalk canonical source evidence is invalid")
    source_digest = _digest(source["content_digest"], label="canonical source content digest")
    for key in ("identity_basis", "stable_identity_columns"):
        values = source[key]
        if not isinstance(values, list) or any(not isinstance(value, str) or not value.strip() for value in values):
            raise CrosswalkCollisionReviewError(f"canonical source {key} is invalid")
    summary = document["summary"]
    if not isinstance(summary, Mapping) or set(summary) != {"approval_required", "collision_count", "pending_review_count"}:
        raise CrosswalkCollisionReviewError("Crosswalk collision summary is invalid")
    if (
        summary["approval_required"] is not True
        or not isinstance(summary["collision_count"], int)
        or summary["collision_count"] < 1
        or summary["pending_review_count"] != summary["collision_count"]
    ):
        raise CrosswalkCollisionReviewError("Crosswalk collision queue summary is invalid")
    items = document["items"]
    if not isinstance(items, list) or len(items) != summary["collision_count"]:
        raise CrosswalkCollisionReviewError("Crosswalk collision queue item count does not match summary")
    safe_items: list[dict[str, Any]] = []
    for index, raw in enumerate(items):
        if not isinstance(raw, Mapping) or set(raw) != {
            "candidate_count", "candidate_digests", "decision_status", "normalized_key_digest", "review_id"
        }:
            raise CrosswalkCollisionReviewError(f"collision item[{index}] fields are invalid")
        candidate_digests = raw["candidate_digests"]
        if not isinstance(candidate_digests, list) or len(candidate_digests) != raw["candidate_count"] or len(candidate_digests) != 2:
            raise CrosswalkCollisionReviewError(f"collision item[{index}] candidates are invalid")
        safe_candidates = [_digest(value, label=f"collision item[{index}] candidate digest") for value in candidate_digests]
        if safe_candidates != sorted(set(safe_candidates)):
            raise CrosswalkCollisionReviewError(f"collision item[{index}] candidates must be sorted and unique")
        item = {
            "candidate_count": 2,
            "candidate_digests": safe_candidates,
            "decision_status": "pending",
            "normalized_key_digest": _digest(raw["normalized_key_digest"], label=f"collision item[{index}] key digest"),
            "review_id": _digest(raw["review_id"], label=f"collision item[{index}] review ID"),
        }
        if item["review_id"] != _review_id(item):
            raise CrosswalkCollisionReviewError(f"collision item[{index}] review ID does not match evidence")
        safe_items.append(item)
    if len({item["review_id"] for item in safe_items}) != len(safe_items) or len({item["normalized_key_digest"] for item in safe_items}) != len(safe_items):
        raise CrosswalkCollisionReviewError("Crosswalk collision review IDs or keys are duplicated")
    return {
        "format": _FORMAT,
        "status": _STATUS,
        "activation": "not-activated",
        "execution_allowed": False,
        "policy": document["policy"],
        "canonical_source": {
            "content_digest": source_digest,
            "identity_basis": list(source["identity_basis"]),
            "stable_identity_columns": list(source["stable_identity_columns"]),
        },
        "summary": {
            "approval_required": True,
            "collision_count": len(safe_items),
            "pending_review_count": len(safe_items),
        },
        "items": safe_items,
    }
