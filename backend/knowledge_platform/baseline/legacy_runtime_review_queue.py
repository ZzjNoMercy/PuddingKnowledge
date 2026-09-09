"""Fail-closed validation for the path-free legacy runtime review queue."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

_FORMAT = "agent-native-knowledge-platform-legacy-runtime-boundary-review-queue/v1"
_STATUS = "PHASE0A_LEGACY_RUNTIME_BOUNDARY_REVIEW_QUEUE_READY_NOT_FROZEN"
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
_MODULE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")
_NON_PORTABLE_RE = re.compile(
    r"(?i)(?:file://|https?://|/(?:Users|private|tmp|var|home|etc|opt|usr|root|Volumes)/|[A-Za-z]:[\\/]|\\\\)"
)


class LegacyRuntimeReviewQueueError(ValueError):
    """Raised when a runtime review queue cannot cross its public boundary."""


def _string(value: object, *, label: str, pattern: re.Pattern[str] | None = None) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise LegacyRuntimeReviewQueueError(f"{label} is invalid")
    if pattern is not None and pattern.fullmatch(value) is None:
        raise LegacyRuntimeReviewQueueError(f"{label} is invalid")
    return value


def _digest(value: object, *, label: str) -> str:
    return _string(value, label=label, pattern=_DIGEST_RE)


def _safe_text(value: object, *, label: str) -> str:
    text = _string(value, label=label)
    if len(text) > 600 or any(ord(character) < 32 and character not in "\t\n\r" for character in text):
        raise LegacyRuntimeReviewQueueError(f"{label} is not portable")
    if _NON_PORTABLE_RE.search(text):
        raise LegacyRuntimeReviewQueueError(f"{label} is not portable")
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


def _validate_module_list(raw: object, *, label: str) -> list[str]:
    if not isinstance(raw, list) or not raw:
        raise LegacyRuntimeReviewQueueError(f"{label} is invalid")
    values = [_string(value, label=f"{label} entry", pattern=_MODULE_RE) for value in raw]
    if len(values) != len(set(values)) or values != sorted(values):
        raise LegacyRuntimeReviewQueueError(f"{label} must be sorted and unique")
    return values


def _validate_item(raw: object, *, index: int) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise LegacyRuntimeReviewQueueError(f"review queue item[{index}] is invalid")
    fields = {
        "byte_equal",
        "capability_id",
        "graph_digest",
        "loaded_module_count",
        "observed_edge_count",
        "observed_required_callee_modules",
        "output_sha256",
        "probe_id",
        "probe_reference",
        "reason",
        "replay_runs",
        "required_callee_modules",
        "review_id",
        "review_status",
    }
    if set(raw) != fields:
        raise LegacyRuntimeReviewQueueError(f"review queue item[{index}] fields are invalid")
    probe_id = _string(raw["probe_id"], label=f"item[{index}].probe_id", pattern=_ID_RE)
    capability_id = _string(raw["capability_id"], label=f"item[{index}].capability_id", pattern=_ID_RE)
    reference = _string(raw["probe_reference"], label=f"item[{index}].probe_reference")
    if reference.count(":") != 1 or any(
        _MODULE_RE.fullmatch(part) is None for part in reference.split(":", 1)
    ):
        raise LegacyRuntimeReviewQueueError(f"item[{index}].probe_reference is invalid")
    required = _validate_module_list(raw["required_callee_modules"], label=f"item[{index}].required_callee_modules")
    observed_required = _validate_module_list(
        raw["observed_required_callee_modules"], label=f"item[{index}].observed_required_callee_modules"
    )
    if set(observed_required) != set(required):
        raise LegacyRuntimeReviewQueueError(f"item[{index}] required callee evidence is incomplete or overreported")
    for key in ("loaded_module_count", "observed_edge_count", "replay_runs"):
        value = raw[key]
        if type(value) is not int or value < 1:
            raise LegacyRuntimeReviewQueueError(f"item[{index}].{key} is invalid")
    if raw["replay_runs"] < 2 or raw["byte_equal"] is not True or raw["review_status"] != "pending":
        raise LegacyRuntimeReviewQueueError(f"item[{index}] replay/review status is invalid")
    item = {
        "byte_equal": True,
        "capability_id": capability_id,
        "graph_digest": _digest(raw["graph_digest"], label=f"item[{index}].graph_digest"),
        "loaded_module_count": raw["loaded_module_count"],
        "observed_edge_count": raw["observed_edge_count"],
        "observed_required_callee_modules": observed_required,
        "output_sha256": _digest(raw["output_sha256"], label=f"item[{index}].output_sha256"),
        "probe_id": probe_id,
        "probe_reference": reference,
        "reason": _safe_text(raw["reason"], label=f"item[{index}].reason"),
        "replay_runs": raw["replay_runs"],
        "required_callee_modules": required,
        "review_id": _digest(raw["review_id"], label=f"item[{index}].review_id"),
        "review_status": "pending",
    }
    if item["review_id"] != review_id_for_item(item):
        raise LegacyRuntimeReviewQueueError(f"item[{index}].review_id does not match evidence")
    return item


def load_legacy_runtime_review_queue(document: object) -> dict[str, Any]:
    if not isinstance(document, Mapping):
        raise LegacyRuntimeReviewQueueError("review queue must be an object")
    required = {"activation", "execution_allowed", "format", "items", "policy", "registry", "replay", "status", "summary"}
    if set(document) != required or document["format"] != _FORMAT or document["status"] != _STATUS:
        raise LegacyRuntimeReviewQueueError("review queue envelope is invalid")
    if document["activation"] != "not-activated" or document["execution_allowed"] is not False:
        raise LegacyRuntimeReviewQueueError("review queue activation policy is invalid")
    policy = _safe_text(document["policy"], label="review queue policy")
    registry = document["registry"]
    if not isinstance(registry, Mapping) or set(registry) != {"coverage_scope", "legacy_runtime_coverage", "sha256", "status"}:
        raise LegacyRuntimeReviewQueueError("review queue registry evidence is invalid")
    registry_sha256 = _digest(registry["sha256"], label="registry.sha256")
    registry_status = _safe_text(registry["status"], label="registry.status")
    coverage_scope = _safe_text(registry["coverage_scope"], label="registry.coverage_scope")
    legacy_coverage = _safe_text(registry["legacy_runtime_coverage"], label="registry.legacy_runtime_coverage")
    replay = document["replay"]
    if not isinstance(replay, Mapping) or set(replay) != {"digest", "replayed_probe_count", "replayed_probe_ids", "status"}:
        raise LegacyRuntimeReviewQueueError("review queue replay evidence is invalid")
    replay_digest = _digest(replay["digest"], label="replay.digest")
    replay_status = _safe_text(replay["status"], label="replay.status")
    replayed_count = replay["replayed_probe_count"]
    replayed_ids = _validate_module_list(replay["replayed_probe_ids"], label="replay.replayed_probe_ids")
    if replay_status != "matched" or type(replayed_count) is not int or replayed_count < 1 or replayed_count != len(replayed_ids):
        raise LegacyRuntimeReviewQueueError("review queue replay evidence is incomplete")
    summary = document["summary"]
    summary_fields = {
        "all_replayed",
        "approval_required",
        "pending_review_count",
        "replayed_probe_count",
        "required_callee_complete",
        "review_item_count",
    }
    if not isinstance(summary, Mapping) or set(summary) != summary_fields:
        raise LegacyRuntimeReviewQueueError("review queue summary is invalid")
    if summary["approval_required"] is not True or summary["all_replayed"] is not True or summary["required_callee_complete"] is not True:
        raise LegacyRuntimeReviewQueueError("review queue summary must remain approval-required")
    items = document["items"]
    if not isinstance(items, list):
        raise LegacyRuntimeReviewQueueError("review queue items are invalid")
    safe_items = [_validate_item(item, index=index) for index, item in enumerate(items)]
    probe_ids = [item["probe_id"] for item in safe_items]
    if len(probe_ids) != len(set(probe_ids)):
        raise LegacyRuntimeReviewQueueError("review queue probe IDs are duplicated")
    for key in ("pending_review_count", "replayed_probe_count", "review_item_count"):
        if type(summary[key]) is not int or summary[key] < 0:
            raise LegacyRuntimeReviewQueueError(f"summary.{key} is invalid")
    if summary["pending_review_count"] != len(safe_items) or summary["review_item_count"] != len(safe_items):
        raise LegacyRuntimeReviewQueueError("review queue summary counts are inconsistent")
    if summary["replayed_probe_count"] != replayed_count or not set(probe_ids).issubset(replayed_ids):
        raise LegacyRuntimeReviewQueueError("review queue replay count is inconsistent")
    return {
        "format": _FORMAT,
        "status": _STATUS,
        "activation": "not-activated",
        "execution_allowed": False,
        "policy": policy,
        "registry": {
            "sha256": registry_sha256,
            "status": registry_status,
            "coverage_scope": coverage_scope,
            "legacy_runtime_coverage": legacy_coverage,
        },
        "replay": {
            "digest": replay_digest,
            "replayed_probe_count": replayed_count,
            "replayed_probe_ids": replayed_ids,
            "status": "matched",
        },
        "summary": {key: summary[key] for key in sorted(summary_fields)},
        "items": safe_items,
    }
