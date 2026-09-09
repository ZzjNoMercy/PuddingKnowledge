"""Create and validate an explicitly approved, host-local Asset binding manifest.

The restore review manifest is evidence, not approval.  This command requires
one or more explicit ``--approve-review-id`` values and writes a host-local
manifest containing the selected paths.  The manifest is intentionally not a
portable Platform response and is never written to the Catalog.  Loading it
later rechecks the Catalog revision, Asset digest, file identity, and every
parent directory's symlink state.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from knowledge_platform.catalog.sqlite_query import SqliteCatalogQueryRepository
from scripts.phase9_local_asset_rebind_shadow import _load_review_manifest

_FORMAT = "agent-knowledge-platform-local-asset-binding-manifest/v1"
_REVIEW_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
_ID = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
_CONFIRMED_DECISION = "human-review-required-content-matched-single-candidate"
_CONFIRMED_STATUS = "unique-content-digest-confirmed-review-required"
_AMBIGUOUS_DECISION = "human-review-required-select-one-candidate"
_AMBIGUOUS_STATUSES = frozenset(
    {
        "ambiguous-content-digest-confirmed-review-required",
        "ambiguous-content-digest-confirmed",
    }
)


class LocalAssetBindingManifestError(ValueError):
    """Raised when a host binding manifest cannot be safely used."""


def _digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return f"sha256:{hasher.hexdigest()}"


def _safe_existing_file(raw_path: object, *, label: str) -> Path:
    if isinstance(raw_path, Path):
        raw_path = str(raw_path)
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise LocalAssetBindingManifestError(f"{label} must be a non-empty path")
    path = Path(raw_path).expanduser().absolute()
    cursor = path.parent
    while True:
        if cursor.is_symlink():
            raise LocalAssetBindingManifestError(f"{label} contains a symlink")
        if cursor.parent == cursor:
            break
        cursor = cursor.parent
    if path.is_symlink():
        raise LocalAssetBindingManifestError(f"{label} contains a symlink")
    if not path.is_file():
        raise LocalAssetBindingManifestError(f"{label} is not a regular file")
    return path


def _safe_output_path(path: Path) -> Path:
    path = path.expanduser().absolute()
    if path.exists() and path.is_symlink():
        raise LocalAssetBindingManifestError("binding manifest output must not be a symlink")
    cursor = path.parent
    while True:
        if cursor.is_symlink():
            raise LocalAssetBindingManifestError("binding manifest output parent contains a symlink")
        if cursor.parent == cursor:
            break
        cursor = cursor.parent
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not path.is_file():
        raise LocalAssetBindingManifestError("binding manifest output is not a regular file")
    return path


def _approved_at(value: str | None) -> str:
    if value is None:
        return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise LocalAssetBindingManifestError("approved-at must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        raise LocalAssetBindingManifestError("approved-at must include a timezone")
    return parsed.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _review_candidate_options(manifest: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Validate all digest-confirmed candidates, including ambiguous choices."""

    options: dict[str, list[dict[str, Any]]] = {}
    for item in manifest["items"]:
        if not isinstance(item, dict):
            raise LocalAssetBindingManifestError("review manifest contains an invalid item")
        decision = item.get("decision")
        if decision not in {_CONFIRMED_DECISION, _AMBIGUOUS_DECISION}:
            continue
        review_id = str(item.get("review_id") or "")
        if not _REVIEW_ID.fullmatch(review_id):
            raise LocalAssetBindingManifestError("review manifest candidate review ID is invalid")
        verification = item.get("candidate_verification")
        if not isinstance(verification, Mapping):
            raise LocalAssetBindingManifestError(f"review {review_id}: candidate verification is invalid")
        candidates = item.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise LocalAssetBindingManifestError(f"review {review_id}: candidates are invalid")
        candidate_count = verification.get("candidate_count")
        confirmed_count = verification.get("content_digest_confirmed_candidate_count")
        if decision == _CONFIRMED_DECISION:
            valid_shape = (
                verification.get("status") == _CONFIRMED_STATUS
                and candidate_count == 1
                and confirmed_count == 1
                and len(candidates) == 1
            )
        else:
            valid_shape = (
                verification.get("status") in _AMBIGUOUS_STATUSES
                and isinstance(candidate_count, int)
                and candidate_count > 1
                and confirmed_count == candidate_count
                and len(candidates) == candidate_count
            )
        if not valid_shape:
            raise LocalAssetBindingManifestError(f"review {review_id}: candidate evidence is inconsistent")
        validated: list[dict[str, Any]] = []
        seen_paths: set[Path] = set()
        for candidate_index, raw_candidate in enumerate(candidates):
            if not isinstance(raw_candidate, Mapping):
                raise LocalAssetBindingManifestError(f"review {review_id}: candidate is invalid")
            path = _safe_existing_file(raw_candidate.get("path"), label=f"review {review_id} candidate")
            if path in seen_paths:
                raise LocalAssetBindingManifestError(f"review {review_id}: candidate paths are duplicated")
            seen_paths.add(path)
            digest = raw_candidate.get("sha256")
            if not isinstance(digest, str) or not _REVIEW_ID.fullmatch(digest):
                raise LocalAssetBindingManifestError(f"review {review_id}: candidate digest is invalid")
            if _digest(path) != digest:
                raise LocalAssetBindingManifestError(f"review {review_id}: candidate file is missing or changed")
            validated.append({"review_id": review_id, "candidate_index": candidate_index, "digest": digest, "path": path})
        if len({entry["digest"] for entry in validated}) != 1:
            raise LocalAssetBindingManifestError(f"review {review_id}: candidate digests are inconsistent")
        if review_id in options:
            raise LocalAssetBindingManifestError(f"review {review_id}: review ID is duplicated")
        options[review_id] = validated
    return options


def build_binding_manifest(
    *,
    review_manifest_path: Path,
    catalog_path: Path,
    output_path: Path,
    approval_review_ids: list[str],
    approved_by: str = "local-user",
    approved_at: str | None = None,
    space_id: str = "space_kb_default",
    selected_candidates: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    if not approval_review_ids:
        raise LocalAssetBindingManifestError("at least one review ID must be explicitly approved")
    if not _ID.fullmatch(approved_by) or not _ID.fullmatch(space_id):
        raise LocalAssetBindingManifestError("approved-by or space-id is invalid")
    approval_ids = set(approval_review_ids)
    if len(approval_ids) != len(approval_review_ids) or any(not _REVIEW_ID.fullmatch(value) for value in approval_ids):
        raise LocalAssetBindingManifestError("approval review IDs must be unique sha256 digests")

    review_manifest_path = _safe_existing_file(review_manifest_path, label="review manifest")
    review_manifest, review_sha256 = _load_review_manifest(review_manifest_path)
    candidate_options = _review_candidate_options(review_manifest)
    by_review_id: dict[str, dict[str, Any]] = {}
    selections = dict(selected_candidates or {})
    if any(not isinstance(key, str) or not _REVIEW_ID.fullmatch(key) for key in selections):
        raise LocalAssetBindingManifestError("selected candidate review IDs are invalid")
    if any(type(value) is not int or value < 0 for value in selections.values()):
        raise LocalAssetBindingManifestError("selected candidate indexes must be non-negative integers")
    if set(selections) - approval_ids:
        raise LocalAssetBindingManifestError("selected candidate review ID is not explicitly approved")
    for review_id in approval_ids:
        options = candidate_options.get(review_id)
        if not options:
            continue
        if len(options) == 1:
            if review_id in selections:
                raise LocalAssetBindingManifestError("candidate selection is only allowed for ambiguous reviews")
            by_review_id[review_id] = options[0]
            continue
        if review_id not in selections:
            raise LocalAssetBindingManifestError(
                f"review {review_id} has multiple candidates; --select-review-candidate is required"
            )
        candidate_index = selections[review_id]
        if candidate_index >= len(options):
            raise LocalAssetBindingManifestError(f"review {review_id} candidate index is out of range")
        by_review_id[review_id] = options[candidate_index]
    unknown = approval_ids - set(by_review_id)
    if unknown:
        raise LocalAssetBindingManifestError("approval includes an unknown or non-confirmed review ID")

    catalog_path = catalog_path.expanduser().absolute()
    repository = SqliteCatalogQueryRepository(catalog_path)
    catalog_revision = repository.catalog_revision
    assets_by_digest: dict[str, list[dict[str, Any]]] = {}
    for asset in repository.list_assets(space_id=space_id):
        assets_by_digest.setdefault(str(asset.get("content_digest") or ""), []).append(dict(asset))

    grouped: dict[str, dict[str, Any]] = {}
    for review_id in sorted(approval_ids):
        entry = by_review_id[review_id]
        digest = str(entry["digest"])
        path = _safe_existing_file(entry["path"], label=f"review {review_id} candidate")
        if _digest(path) != digest:
            raise LocalAssetBindingManifestError(f"review {review_id} candidate digest changed")
        matched_assets = assets_by_digest.get(digest, [])
        if not matched_assets:
            raise LocalAssetBindingManifestError(f"review {review_id} has no matching Catalog Asset")
        if len(matched_assets) != 1:
            raise LocalAssetBindingManifestError(
                f"review {review_id} digest maps to multiple Catalog Assets; explicit Asset selection is required"
            )
        for asset in matched_assets:
            asset_id = str(asset.get("id") or "")
            if not _ID.fullmatch(asset_id) or str(asset.get("space_id") or "") != space_id:
                raise LocalAssetBindingManifestError(f"review {review_id} matched an invalid Catalog Asset")
            current = grouped.setdefault(
                asset_id,
                {
                    "asset_id": asset_id,
                    "space_id": space_id,
                    "content_digest": digest,
                    "bytes": path.stat().st_size,
                    "path": str(path),
                    "approval_review_ids": [],
                },
            )
            if current["content_digest"] != digest or current["path"] != str(path):
                raise LocalAssetBindingManifestError("one Asset was approved with conflicting binding evidence")
            current["approval_review_ids"].append(review_id)

    if repository.catalog_revision != catalog_revision:
        raise LocalAssetBindingManifestError("Catalog changed while preparing bindings")

    result = {
        "format": _FORMAT,
        "status": "APPROVED_HOST_BINDINGS",
        "activation": "not-activated",
        "execution_allowed": False,
        "persistence": "host-manifest-only",
        "approved_by": approved_by,
        "approved_at": _approved_at(approved_at),
        "review_manifest": {
            "path": str(review_manifest_path),
            "sha256": review_sha256,
            "status": review_manifest["status"],
        },
        "catalog": {"revision": catalog_revision, "space_id": space_id},
        "summary": {
            "approved_review_item_count": len(approval_ids),
            "binding_asset_count": len(grouped),
            "policy": "explicit human approval is required; this manifest does not activate or rewrite the Catalog",
        },
        "bindings": [
            {**entry, "approval_review_ids": sorted(set(entry["approval_review_ids"]))}
            for entry in sorted(grouped.values(), key=lambda item: item["asset_id"])
        ],
    }
    output_path = _safe_output_path(output_path)
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


def load_binding_manifest(
    *, manifest_path: Path, catalog_path: Path, space_id: str = "space_kb_default"
) -> dict[str, Path]:
    manifest_path = _safe_existing_file(manifest_path, label="binding manifest")
    try:
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise LocalAssetBindingManifestError("binding manifest cannot be read") from error
    if not isinstance(document, dict) or document.get("format") != _FORMAT:
        raise LocalAssetBindingManifestError("binding manifest format is unsupported")
    if document.get("status") != "APPROVED_HOST_BINDINGS" or document.get("activation") != "not-activated":
        raise LocalAssetBindingManifestError("binding manifest is not an inactive approved manifest")
    if document.get("execution_allowed") is not False or document.get("persistence") != "host-manifest-only":
        raise LocalAssetBindingManifestError("binding manifest execution policy is invalid")
    review_manifest_record = document.get("review_manifest")
    if not isinstance(review_manifest_record, Mapping):
        raise LocalAssetBindingManifestError("binding manifest review evidence is invalid")
    bindings = document.get("bindings")
    if not isinstance(bindings, list) or not bindings:
        raise LocalAssetBindingManifestError("binding manifest has no bindings")
    review_path = _safe_existing_file(review_manifest_record.get("path"), label="review manifest")
    try:
        review_manifest, review_sha256 = _load_review_manifest(review_path)
    except ValueError as error:
        raise LocalAssetBindingManifestError("review manifest is no longer valid") from error
    if (
        review_sha256 != review_manifest_record.get("sha256")
        or review_manifest["status"] != review_manifest_record.get("status")
    ):
        raise LocalAssetBindingManifestError("review manifest changed since binding approval")
    approved_review_ids = {
        str(value)
        for binding in bindings
        if isinstance(binding, Mapping)
        for value in binding.get("approval_review_ids", [])
    }
    try:
        candidate_options = _review_candidate_options(review_manifest)
    except ValueError as error:
        raise LocalAssetBindingManifestError("review manifest candidate evidence is no longer valid") from error
    if not approved_review_ids.issubset(candidate_options):
        raise LocalAssetBindingManifestError("binding approval evidence is no longer confirmed")

    catalog = document.get("catalog")
    if not isinstance(catalog, Mapping) or catalog.get("space_id") != space_id:
        raise LocalAssetBindingManifestError("binding manifest Space does not match the requested Space")

    catalog_path = catalog_path.expanduser().absolute()
    repository = SqliteCatalogQueryRepository(catalog_path)
    expected_revision = str(catalog.get("revision") or "")
    if repository.catalog_revision != expected_revision:
        raise LocalAssetBindingManifestError("Catalog revision changed since binding approval")
    result: dict[str, Path] = {}
    consumed_review_ids: set[str] = set()
    for index, raw in enumerate(bindings):
        if not isinstance(raw, Mapping):
            raise LocalAssetBindingManifestError(f"binding[{index}] is invalid")
        required = {"asset_id", "space_id", "content_digest", "bytes", "path", "approval_review_ids"}
        if set(raw) != required:
            raise LocalAssetBindingManifestError(f"binding[{index}] fields are invalid")
        asset_id = raw["asset_id"]
        digest = raw["content_digest"]
        if not isinstance(asset_id, str) or not _ID.fullmatch(asset_id) or raw["space_id"] != space_id:
            raise LocalAssetBindingManifestError(f"binding[{index}] identity is invalid")
        if not isinstance(digest, str) or not _REVIEW_ID.fullmatch(digest):
            raise LocalAssetBindingManifestError(f"binding[{index}] content digest is invalid")
        if type(raw["bytes"]) is not int or raw["bytes"] < 0:
            raise LocalAssetBindingManifestError(f"binding[{index}] byte count is invalid")
        if not isinstance(raw["approval_review_ids"], list) or not raw["approval_review_ids"] or any(
            not isinstance(value, str) or not _REVIEW_ID.fullmatch(value) for value in raw["approval_review_ids"]
        ):
            raise LocalAssetBindingManifestError(f"binding[{index}] approval evidence is invalid")
        path = _safe_existing_file(raw["path"], label=f"binding[{index}] path")
        binding_review_ids = raw["approval_review_ids"]
        if len(set(binding_review_ids)) != len(binding_review_ids):
            raise LocalAssetBindingManifestError(f"binding[{index}] approval evidence is duplicated")
        for review_id in binding_review_ids:
            candidates = candidate_options.get(review_id)
            if candidates is None:
                raise LocalAssetBindingManifestError(f"binding[{index}] approval evidence is no longer confirmed")
            matching_candidates = [candidate for candidate in candidates if candidate["digest"] == digest]
            if not matching_candidates:
                raise LocalAssetBindingManifestError(
                    f"binding[{index}] approval evidence does not match the binding digest"
                )
            if not any(candidate["path"] == path for candidate in matching_candidates):
                raise LocalAssetBindingManifestError(
                    f"binding[{index}] approval evidence does not match the binding path"
                )
            if review_id in consumed_review_ids:
                raise LocalAssetBindingManifestError("binding approval evidence is reused across Assets")
            consumed_review_ids.add(review_id)
        if asset_id in result:
            raise LocalAssetBindingManifestError("binding manifest contains duplicate Asset IDs")
        if path.stat().st_size != raw["bytes"] or _digest(path) != digest:
            raise LocalAssetBindingManifestError(f"binding[{index}] file digest or size changed")
        asset = repository.get_asset(asset_id=asset_id)
        if (
            asset is None
            or str(asset.get("space_id") or "") != space_id
            or str(asset.get("content_digest") or "") != digest
        ):
            raise LocalAssetBindingManifestError(f"binding[{index}] no longer matches the Catalog Asset")
        result[asset_id] = path
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--review-manifest", type=Path, default=Path("artifacts/phase0b-local-catalog/restore-rebind-review.json")
    )
    parser.add_argument(
        "--catalog", type=Path, default=Path("artifacts/phase0b-local-catalog/knowledge-platform.sqlite3")
    )
    parser.add_argument(
        "--output", type=Path, default=Path("artifacts/phase0b-local-catalog/local-asset-bindings.local.json")
    )
    parser.add_argument("--approve-review-id", action="append", required=True)
    parser.add_argument(
        "--select-review-candidate",
        action="append",
        default=[],
        metavar="REVIEW_ID=INDEX",
        help="select a zero-based candidate index for an ambiguous approved review item",
    )
    parser.add_argument("--approved-by", default="local-user")
    parser.add_argument("--approved-at")
    parser.add_argument("--space-id", default="space_kb_default")
    args = parser.parse_args()
    selected_candidates: dict[str, int] = {}
    for selection in args.select_review_candidate:
        review_id, separator, raw_index = selection.partition("=")
        if not separator or not review_id or not raw_index.isdigit():
            parser.error("--select-review-candidate must use REVIEW_ID=INDEX with a zero-based integer INDEX")
        if review_id in selected_candidates:
            parser.error("--select-review-candidate cannot repeat a REVIEW_ID")
        selected_candidates[review_id] = int(raw_index)
    result = build_binding_manifest(
        review_manifest_path=args.review_manifest,
        catalog_path=args.catalog,
        output_path=args.output,
        approval_review_ids=args.approve_review_id,
        approved_by=args.approved_by,
        approved_at=args.approved_at,
        space_id=args.space_id,
        selected_candidates=selected_candidates,
    )
    print(json.dumps({"status": result["status"], "summary": result["summary"], "output": str(args.output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
