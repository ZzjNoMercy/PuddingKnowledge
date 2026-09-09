"""Fail-closed validation for the path-free platform runtime review queue."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

_FORMAT = "agent-native-knowledge-platform-platform-runtime-boundary-review-queue/v1"
_STATUS = "PHASE0A_PLATFORM_RUNTIME_BOUNDARY_REVIEW_QUEUE_READY_NOT_FROZEN"
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
_FAMILY_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")
_REFERENCE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*$")
_NON_PORTABLE_RE = re.compile(
    r"(?i)(?:file://|https?://|/(?:Users|private|tmp|var|home|etc|opt|usr|root|Volumes)/|[A-Za-z]:[\\/]|\\\\)"
)


class RuntimeReviewQueueError(ValueError):
    """Raised when a platform runtime review queue crosses its public boundary."""


def _string(value: object, *, label: str, pattern: re.Pattern[str] | None = None) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise RuntimeReviewQueueError(f"{label} is invalid")
    if pattern is not None and pattern.fullmatch(value) is None:
        raise RuntimeReviewQueueError(f"{label} is invalid")
    return value


def _digest(value: object, *, label: str) -> str:
    return _string(value, label=label, pattern=_DIGEST_RE)


def _safe_text(value: object, *, label: str) -> str:
    text = _string(value, label=label)
    if len(text) > 600 or any(ord(character) < 32 and character not in "\t\n\r" for character in text):
        raise RuntimeReviewQueueError(f"{label} is not portable")
    if _NON_PORTABLE_RE.search(text):
        raise RuntimeReviewQueueError(f"{label} is not portable")
    return text


def _canonical_review_payload(item: Mapping[str, object]) -> bytes:
    return json.dumps(
        {key: item[key] for key in item if key != "review_id"},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def review_id_for_item(item: Mapping[str, object]) -> str:
    return f"sha256:{hashlib.sha256(_canonical_review_payload(item)).hexdigest()}"


def _validate_item(raw: object, *, index: int) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise RuntimeReviewQueueError(f"review queue item[{index}] is invalid")
    fields = {
        "byte_equal",
        "family",
        "graph_digest",
        "loaded_module_count",
        "observed_edge_count",
        "output_sha256",
        "probe_id",
        "probe_reference",
        "reason",
        "replay_runs",
        "review_id",
        "review_status",
    }
    if set(raw) != fields:
        raise RuntimeReviewQueueError(f"review queue item[{index}] fields are invalid")
    probe_id = _string(raw["probe_id"], label=f"item[{index}].probe_id", pattern=_ID_RE)
    family = _string(raw["family"], label=f"item[{index}].family", pattern=_FAMILY_RE)
    reference = _string(raw["probe_reference"], label=f"item[{index}].probe_reference", pattern=_REFERENCE_RE)
    for key in ("loaded_module_count", "observed_edge_count"):
        value = raw[key]
        if type(value) is not int or value < 1:
            raise RuntimeReviewQueueError(f"item[{index}].{key} is invalid")
    replay_runs = raw["replay_runs"]
    if type(replay_runs) is not int or replay_runs < 1:
        raise RuntimeReviewQueueError(f"item[{index}].replay_runs is invalid")
    if replay_runs < 1 or raw["byte_equal"] is not True or raw["review_status"] != "pending":
        raise RuntimeReviewQueueError(f"item[{index}] replay/review status is invalid")
    item = {
        "byte_equal": True,
        "family": family,
        "graph_digest": _digest(raw["graph_digest"], label=f"item[{index}].graph_digest"),
        "loaded_module_count": raw["loaded_module_count"],
        "observed_edge_count": raw["observed_edge_count"],
        "output_sha256": _digest(raw["output_sha256"], label=f"item[{index}].output_sha256"),
        "probe_id": probe_id,
        "probe_reference": reference,
        "reason": _safe_text(raw["reason"], label=f"item[{index}].reason"),
        "replay_runs": replay_runs,
        "review_id": _digest(raw["review_id"], label=f"item[{index}].review_id"),
        "review_status": "pending",
    }
    if item["review_id"] != review_id_for_item(item):
        raise RuntimeReviewQueueError(f"item[{index}].review_id does not match evidence")
    return item


def load_runtime_review_queue(document: object) -> dict[str, Any]:
    """Validate and normalize the approval-only queue document."""

    if not isinstance(document, Mapping):
        raise RuntimeReviewQueueError("review queue must be an object")
    required = {"activation", "execution_allowed", "format", "items", "policy", "registry", "replay", "status", "summary"}
    if set(document) != required or document["format"] != _FORMAT or document["status"] != _STATUS:
        raise RuntimeReviewQueueError("review queue envelope is invalid")
    if document["activation"] != "not-activated" or document["execution_allowed"] is not False:
        raise RuntimeReviewQueueError("review queue activation policy is invalid")
    policy = _safe_text(document["policy"], label="review queue policy")
    registry = document["registry"]
    registry_fields = {"coverage_scope", "platform_runtime_coverage", "sha256", "status"}
    if not isinstance(registry, Mapping) or set(registry) != registry_fields:
        raise RuntimeReviewQueueError("review queue registry evidence is invalid")
    registry_evidence = {
        "sha256": _digest(registry["sha256"], label="registry.sha256"),
        "status": _safe_text(registry["status"], label="registry.status"),
        "coverage_scope": _safe_text(registry["coverage_scope"], label="registry.coverage_scope"),
        "platform_runtime_coverage": _safe_text(
            registry["platform_runtime_coverage"], label="registry.platform_runtime_coverage"
        ),
    }
    replay = document["replay"]
    replay_fields = {"digest", "replayed_probe_count", "replayed_probe_ids", "status"}
    if not isinstance(replay, Mapping) or set(replay) != replay_fields:
        raise RuntimeReviewQueueError("review queue replay evidence is invalid")
    replayed_ids = replay["replayed_probe_ids"]
    if not isinstance(replayed_ids, list) or any(
        not isinstance(value, str) or _ID_RE.fullmatch(value) is None for value in replayed_ids
    ) or replayed_ids != sorted(set(replayed_ids)):
        raise RuntimeReviewQueueError("replay.replayed_probe_ids must be sorted and unique")
    replayed_count = replay["replayed_probe_count"]
    if replay["status"] != "matched" or type(replayed_count) is not int or replayed_count != len(replayed_ids):
        raise RuntimeReviewQueueError("review queue replay evidence is incomplete")
    replay_evidence = {
        "digest": _digest(replay["digest"], label="replay.digest"),
        "replayed_probe_count": replayed_count,
        "replayed_probe_ids": replayed_ids,
        "status": "matched",
    }
    summary = document["summary"]
    summary_fields = {"all_replayed", "approval_required", "pending_review_count", "replayed_probe_count", "review_item_count"}
    if not isinstance(summary, Mapping) or set(summary) != summary_fields:
        raise RuntimeReviewQueueError("review queue summary is invalid")
    if summary["approval_required"] is not True or summary["all_replayed"] is not True:
        raise RuntimeReviewQueueError("review queue summary must remain approval-required")
    items = document["items"]
    if not isinstance(items, list):
        raise RuntimeReviewQueueError("review queue items are invalid")
    safe_items = [_validate_item(item, index=index) for index, item in enumerate(items)]
    safe_items = sorted(safe_items, key=lambda item: item["probe_id"])
    probe_ids = [item["probe_id"] for item in safe_items]
    if len(probe_ids) != len(set(probe_ids)):
        raise RuntimeReviewQueueError("review queue probe IDs are duplicated")
    for key in ("pending_review_count", "replayed_probe_count", "review_item_count"):
        if type(summary[key]) is not int or summary[key] < 0:
            raise RuntimeReviewQueueError(f"summary.{key} is invalid")
    if summary["pending_review_count"] != len(safe_items) or summary["review_item_count"] != len(safe_items):
        raise RuntimeReviewQueueError("review queue summary counts are inconsistent")
    if summary["replayed_probe_count"] != replayed_count or not set(probe_ids).issubset(replayed_ids):
        raise RuntimeReviewQueueError("review queue replay count is inconsistent")
    return {
        "format": _FORMAT,
        "status": _STATUS,
        "activation": "not-activated",
        "execution_allowed": False,
        "policy": policy,
        "registry": registry_evidence,
        "replay": replay_evidence,
        "summary": {key: summary[key] for key in sorted(summary_fields)},
        "items": safe_items,
    }
