"""Validate a path-free Golden dependency decision queue."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

_FORMAT = "agent-knowledge-platform-golden-dependency-review-queue/v1"
_STATUS = "PHASE0A_GOLDEN_DEPENDENCY_REVIEW_REQUIRED_NOT_FROZEN"
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
_PATH_RE = re.compile(r"^[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.:-]+)+$")
_NON_PORTABLE_RE = re.compile(
    r"(?i)(?:file://|https?://|/(?:Users|private|tmp|var|home|etc|opt|usr|root|Volumes)/|[A-Za-z]:[\\/]|\\\\)"
)


class GoldenDependencyReviewError(ValueError):
    """Raised when a Golden decision queue is unsafe or internally inconsistent."""


def _string(value: object, *, label: str, pattern: re.Pattern[str] | None = None) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise GoldenDependencyReviewError(f"{label} is invalid")
    if pattern is not None and pattern.fullmatch(value) is None:
        raise GoldenDependencyReviewError(f"{label} is invalid")
    return value


def _digest(value: object, *, label: str) -> str:
    return _string(value, label=label, pattern=_DIGEST_RE)


def _portable_text(value: object, *, label: str) -> str:
    text = _string(value, label=label)
    if len(text) > 600 or any(ord(character) < 32 and character not in "\t\n\r" for character in text):
        raise GoldenDependencyReviewError(f"{label} is not portable")
    if _NON_PORTABLE_RE.search(text):
        raise GoldenDependencyReviewError(f"{label} is not portable")
    return text


def _review_id(item: Mapping[str, object]) -> str:
    payload = {key: item[key] for key in item if key != "review_id"}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def load_golden_dependency_review_queue(document: object) -> dict[str, Any]:
    if not isinstance(document, Mapping):
        raise GoldenDependencyReviewError("Golden dependency queue must be an object")
    required = {"activation", "execution_allowed", "format", "items", "policy", "replay", "status", "summary"}
    if set(document) != required or document["format"] != _FORMAT or document["status"] != _STATUS:
        raise GoldenDependencyReviewError("Golden dependency queue envelope is invalid")
    if document["activation"] != "not-activated" or document["execution_allowed"] is not False:
        raise GoldenDependencyReviewError("Golden dependency queue activation policy is invalid")
    policy = _portable_text(document["policy"], label="queue policy")
    replay = document["replay"]
    if not isinstance(replay, Mapping) or set(replay) != {"mismatch_count", "report_sha256", "status"}:
        raise GoldenDependencyReviewError("Golden replay evidence is invalid")
    if replay["status"] != "mismatch" or type(replay["mismatch_count"]) is not int or replay["mismatch_count"] < 1:
        raise GoldenDependencyReviewError("Golden replay evidence must remain mismatched")
    report_sha256 = _digest(replay["report_sha256"], label="replay.report_sha256")
    summary = document["summary"]
    if not isinstance(summary, Mapping) or set(summary) != {"approval_required", "pending_review_count", "review_item_count"}:
        raise GoldenDependencyReviewError("Golden dependency summary is invalid")
    if summary["approval_required"] is not True or summary["pending_review_count"] != 1 or summary["review_item_count"] != 1:
        raise GoldenDependencyReviewError("Golden dependency queue must contain one pending approval")
    items = document["items"]
    if not isinstance(items, list) or len(items) != 1:
        raise GoldenDependencyReviewError("Golden dependency queue must contain one item")
    raw = items[0]
    if not isinstance(raw, Mapping):
        raise GoldenDependencyReviewError("Golden dependency item is invalid")
    fields = {
        "candidate_record_sha256",
        "canonical_record_sha256",
        "capability_id",
        "decision_status",
        "dependency_digest_drifts",
        "differing_fields",
        "options",
        "review_id",
    }
    if set(raw) != fields:
        raise GoldenDependencyReviewError("Golden dependency item fields are invalid")
    capability_id = _string(raw["capability_id"], label="capability_id", pattern=_ID_RE)
    candidate_sha = _digest(raw["candidate_record_sha256"], label="candidate_record_sha256")
    canonical_sha = _digest(raw["canonical_record_sha256"], label="canonical_record_sha256")
    differing_fields = raw["differing_fields"]
    if not isinstance(differing_fields, list) or not differing_fields or any(
        not isinstance(field, str) or not field.strip() for field in differing_fields
    ) or differing_fields != sorted(set(differing_fields)):
        raise GoldenDependencyReviewError("Golden differing_fields are invalid")
    drifts = raw["dependency_digest_drifts"]
    if not isinstance(drifts, list) or not drifts:
        raise GoldenDependencyReviewError("Golden dependency drifts are invalid")
    safe_drifts: list[dict[str, str]] = []
    for drift in drifts:
        if not isinstance(drift, Mapping) or set(drift) != {"expected", "observed", "path"}:
            raise GoldenDependencyReviewError("Golden dependency drift fields are invalid")
        safe_drifts.append(
            {
                "expected": _digest(drift["expected"], label="dependency expected digest"),
                "observed": _digest(drift["observed"], label="dependency observed digest"),
                "path": _string(drift["path"], label="dependency path", pattern=_PATH_RE),
            }
        )
    if safe_drifts != sorted(safe_drifts, key=lambda value: value["path"]):
        raise GoldenDependencyReviewError("Golden dependency drifts must be sorted")
    options = raw["options"]
    if not isinstance(options, list) or len(options) != 2:
        raise GoldenDependencyReviewError("Golden dependency options are invalid")
    safe_options: list[dict[str, str]] = []
    for option in options:
        if not isinstance(option, Mapping) or set(option) != {"description", "id"}:
            raise GoldenDependencyReviewError("Golden dependency option fields are invalid")
        safe_options.append(
            {
                "id": _string(option["id"], label="dependency option id", pattern=_ID_RE),
                "description": _portable_text(option["description"], label="dependency option description"),
            }
        )
    if [option["id"] for option in safe_options] != ["include_observer", "exclude_observer"]:
        raise GoldenDependencyReviewError("Golden dependency options must be explicit and ordered")
    item = {
        "candidate_record_sha256": candidate_sha,
        "canonical_record_sha256": canonical_sha,
        "capability_id": capability_id,
        "decision_status": "pending",
        "dependency_digest_drifts": safe_drifts,
        "differing_fields": list(differing_fields),
        "options": safe_options,
        "review_id": _digest(raw["review_id"], label="review_id"),
    }
    if item["review_id"] != _review_id(item):
        raise GoldenDependencyReviewError("Golden dependency review_id does not match evidence")
    return {
        "format": _FORMAT,
        "status": _STATUS,
        "activation": "not-activated",
        "execution_allowed": False,
        "policy": policy,
        "replay": {"mismatch_count": replay["mismatch_count"], "report_sha256": report_sha256, "status": "mismatch"},
        "summary": {"approval_required": True, "pending_review_count": 1, "review_item_count": 1},
        "items": [item],
    }
