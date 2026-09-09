"""Read-only, path-free access to the local Asset binding review queue."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from knowledge_contracts import Correlation, Principal, QueryError, QueryErrorCode, QueryResult

_FORMAT = "agent-knowledge-platform-local-asset-binding-review-queue/v1"
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
_PORTABLE_LABEL_RE = re.compile(r"(?i)(?:file://|https?://|/(?:Users|private|tmp|var|home|etc|opt|usr|root|Volumes)/|[A-Za-z]:[\\/]|\\\\)")


class LocalAssetBindingReviewQueueError(ValueError):
    """Raised when a queue cannot be exposed at the public boundary."""


def _string(value: object, *, label: str, pattern: re.Pattern[str] | None = None) -> str:
    if not isinstance(value, str) or not value.strip() or (pattern is not None and not pattern.fullmatch(value)):
        raise LocalAssetBindingReviewQueueError(f"{label} is invalid")
    return value


def _safe_label(value: object, *, label: str) -> str:
    text = _string(value, label=label)
    if len(text) > 240 or any(ord(character) < 32 and character not in "\t\n\r" for character in text):
        raise LocalAssetBindingReviewQueueError(f"{label} is not portable")
    if _PORTABLE_LABEL_RE.search(text):
        raise LocalAssetBindingReviewQueueError(f"{label} is not portable")
    return text


def _safe_queue_path(path: Path) -> Path:
    candidate = path.expanduser().absolute()
    cursor = candidate
    while True:
        if cursor.is_symlink():
            raise LocalAssetBindingReviewQueueError("review queue path contains a symlink")
        if cursor.parent == cursor:
            break
        cursor = cursor.parent
    if not candidate.is_file():
        raise LocalAssetBindingReviewQueueError("review queue file is unavailable")
    return candidate


def _load_path_free_queue(path: Path, *, space_id: str) -> dict[str, Any]:
    queue_path = _safe_queue_path(path)
    try:
        raw = json.loads(queue_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise LocalAssetBindingReviewQueueError("review queue cannot be read") from error
    if not isinstance(raw, Mapping):
        raise LocalAssetBindingReviewQueueError("review queue must be an object")
    required = {"activation", "catalog", "execution_allowed", "format", "items", "policy", "status", "review_manifest", "summary"}
    if set(raw) != required:
        raise LocalAssetBindingReviewQueueError("review queue fields are invalid")
    if raw["format"] != _FORMAT or raw["status"] != "PHASE9_LOCAL_ASSET_BINDING_REVIEW_QUEUE_READY_NOT_ACTIVATABLE":
        raise LocalAssetBindingReviewQueueError("review queue status is invalid")
    if raw["activation"] != "not-activated" or raw["execution_allowed"] is not False:
        raise LocalAssetBindingReviewQueueError("review queue activation policy is invalid")
    policy = _safe_label(raw["policy"], label="review queue policy")
    catalog = raw["catalog"]
    if not isinstance(catalog, Mapping) or set(catalog) != {
        "canonical_sha256_after", "canonical_sha256_before", "canonical_unchanged", "revision", "space_id"
    }:
        raise LocalAssetBindingReviewQueueError("review queue Catalog evidence is invalid")
    catalog_space = _string(catalog["space_id"], label="review queue Catalog Space", pattern=_ID_RE)
    if catalog_space != space_id or catalog["canonical_unchanged"] is not True:
        raise LocalAssetBindingReviewQueueError("review queue Catalog Space or immutability evidence is invalid")
    catalog_revision = _string(catalog["revision"], label="review queue Catalog revision", pattern=_DIGEST_RE)
    for key in ("canonical_sha256_before", "canonical_sha256_after"):
        _string(catalog[key], label=f"review queue {key}", pattern=_DIGEST_RE)
    review_manifest = raw["review_manifest"]
    if not isinstance(review_manifest, Mapping) or set(review_manifest) != {"sha256", "status"}:
        raise LocalAssetBindingReviewQueueError("review queue review evidence is invalid")
    review_sha256 = _string(review_manifest["sha256"], label="review manifest digest", pattern=_DIGEST_RE)
    review_status = _safe_label(review_manifest["status"], label="review manifest status")
    summary = raw["summary"]
    summary_fields = {
        "approval_required", "ambiguous_candidate_item_count", "confirmed_candidate_item_count", "matched_candidate_item_count",
        "matched_catalog_asset_count", "review_item_count", "unique_candidate_digest_count",
    }
    if not isinstance(summary, Mapping) or set(summary) != summary_fields or summary["approval_required"] is not True:
        raise LocalAssetBindingReviewQueueError("review queue summary is invalid")
    for key in summary_fields - {"approval_required"}:
        if type(summary[key]) is not int or summary[key] < 0:
            raise LocalAssetBindingReviewQueueError("review queue summary counts are invalid")
    if summary["ambiguous_candidate_item_count"] > summary["confirmed_candidate_item_count"]:
        raise LocalAssetBindingReviewQueueError("review queue ambiguous candidate count is invalid")
    items = raw["items"]
    if not isinstance(items, list) or len(items) != summary["confirmed_candidate_item_count"]:
        raise LocalAssetBindingReviewQueueError("review queue items are invalid")
    safe_items: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        if not isinstance(item, Mapping) or set(item) != {
            "approval_required", "bytes", "candidate_count", "candidate_selection_required",
            "catalog_asset_ids", "catalog_asset_match_count", "catalog_assets", "content_digest", "review_id"
        }:
            raise LocalAssetBindingReviewQueueError(f"review queue item[{index}] fields are invalid")
        if (
            item["approval_required"] is not True
            or type(item["bytes"]) is not int
            or item["bytes"] < 0
            or type(item["candidate_count"]) is not int
            or item["candidate_count"] < 1
            or type(item["candidate_selection_required"]) is not bool
            or item["candidate_selection_required"] != (item["candidate_count"] > 1)
        ):
            raise LocalAssetBindingReviewQueueError(f"review queue item[{index}] evidence is invalid")
        review_id = _string(item["review_id"], label=f"review queue item[{index}] review_id", pattern=_DIGEST_RE)
        digest = _string(item["content_digest"], label=f"review queue item[{index}] content_digest", pattern=_DIGEST_RE)
        asset_ids = item["catalog_asset_ids"]
        assets = item["catalog_assets"]
        if not isinstance(asset_ids, list) or any(not isinstance(value, str) or not _ID_RE.fullmatch(value) for value in asset_ids):
            raise LocalAssetBindingReviewQueueError(f"review queue item[{index}] Asset IDs are invalid")
        if not isinstance(assets, list) or len(assets) != len(asset_ids) or item["catalog_asset_match_count"] != len(asset_ids):
            raise LocalAssetBindingReviewQueueError(f"review queue item[{index}] Asset matches are invalid")
        safe_assets = []
        for asset_index, asset in enumerate(assets):
            if not isinstance(asset, Mapping) or set(asset) != {"asset_id", "kind", "mime_type", "title"}:
                raise LocalAssetBindingReviewQueueError(f"review queue item[{index}] Asset metadata is invalid")
            asset_id = _string(asset["asset_id"], label="review queue Asset ID", pattern=_ID_RE)
            if asset_id != asset_ids[asset_index]:
                raise LocalAssetBindingReviewQueueError(f"review queue item[{index}] Asset identity is inconsistent")
            safe_assets.append({
                "asset_id": asset_id,
                "kind": _safe_label(asset["kind"], label="review queue Asset kind"),
                "mime_type": _safe_label(asset["mime_type"], label="review queue Asset MIME type"),
                "title": _safe_label(asset["title"], label="review queue Asset title"),
            })
        safe_items.append({
            "approval_required": True,
            "bytes": item["bytes"],
            "candidate_count": item["candidate_count"],
            "candidate_selection_required": item["candidate_selection_required"],
            "catalog_asset_ids": list(asset_ids),
            "catalog_asset_match_count": len(asset_ids),
            "catalog_assets": safe_assets,
            "content_digest": digest,
            "review_id": review_id,
        })
    if len({item["review_id"] for item in safe_items}) != len(safe_items):
        raise LocalAssetBindingReviewQueueError("review queue review IDs are duplicated")
    if sum(item["candidate_selection_required"] for item in safe_items) != summary["ambiguous_candidate_item_count"]:
        raise LocalAssetBindingReviewQueueError("review queue ambiguous candidate evidence is inconsistent")
    return {
        "format": _FORMAT,
        "status": "PHASE9_LOCAL_ASSET_BINDING_REVIEW_QUEUE_READY_NOT_ACTIVATABLE",
        "activation": "not-activated",
        "execution_allowed": False,
        "review_manifest": {"sha256": review_sha256, "status": review_status},
        "catalog": {"revision": catalog_revision, "space_id": catalog_space, "canonical_unchanged": True},
        "summary": {key: summary[key] for key in sorted(summary_fields)},
        "policy": policy,
        "items": safe_items,
    }


class LocalAssetBindingReviewQueue:
    """Expose a prebuilt local queue without exposing its candidate paths."""

    def __init__(self, queue_path: Path) -> None:
        self._queue_path = Path(queue_path)

    def list(self, *, principal: Principal, correlation: Correlation, space_id: str) -> QueryResult:
        if principal.tenant_id is not None or not {"knowledge.admin", "knowledge:admin"} & set(principal.scopes):
            return QueryResult(
                status="error",
                trace_id=correlation.trace_id,
                answer="",
                data={},
                error=QueryError(code=QueryErrorCode.PERMISSION_DENIED, message="knowledge.admin scope is required"),
            )
        if not isinstance(space_id, str) or not _ID_RE.fullmatch(space_id):
            return QueryResult(
                status="error",
                trace_id=correlation.trace_id,
                error=QueryError(code=QueryErrorCode.INVALID_REQUEST, message="space_id is invalid"),
            )
        try:
            queue = _load_path_free_queue(self._queue_path, space_id=space_id)
        except LocalAssetBindingReviewQueueError:
            return QueryResult(
                status="error",
                trace_id=correlation.trace_id,
                error=QueryError(
                    code=QueryErrorCode.CAPABILITY_UNAVAILABLE,
                    message="local Asset binding review queue is unavailable",
                ),
            )
        return QueryResult(
            status="ok",
            trace_id=correlation.trace_id,
            answer="本地 Asset binding 队列仅供人工复核；未批准、未激活、未写入 Catalog。",
            data={"asset_binding_review_queue": queue},
        )
