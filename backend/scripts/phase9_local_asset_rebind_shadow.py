"""Verify explicit local file bindings through the Platform Asset read boundary.

This command is deliberately a Shadow verifier, not a restore/rebind executor.
It consumes the human-review-only manifest produced by
``phase0b_restore_review_manifest.py`` and tests only single-candidate,
content-digest-confirmed entries.  The physical path is used by the local host
as an explicit binding and is never emitted in the result.  Neither the source
Catalog nor the staged Catalog is modified.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from knowledge_contracts import Correlation, Principal
from knowledge_platform.catalog.sqlite_query import SqliteCatalogQueryRepository
from knowledge_platform.retrieval.local import LocalFilesystemBlobReader
from knowledge_platform.retrieval.services import AssetReadService

_REVIEW_FORMAT = "agent-knowledge-platform-restore-review/v1"
_SHADOW_FORMAT = "agent-knowledge-platform-local-asset-rebind-shadow/v1"
_CONFIRMED_DECISION = "human-review-required-content-matched-single-candidate"
_CONFIRMED_STATUS = "unique-content-digest-confirmed-review-required"
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_PORTABLE_PATH = re.compile(
    r"(?i)(?:file://|(?<![A-Za-z0-9_])[A-Za-z]:[\\/]|\\\\|/(?:Users|private|tmp|var|home|etc|opt|usr|root|mnt|Applications|System|Volumes)/)"
)


def _sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _sha256(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _require_dict(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _load_review_manifest(path: Path) -> tuple[dict[str, Any], str]:
    path = path.expanduser().absolute()
    if path.is_symlink():
        raise ValueError("review manifest must not be a symlink")
    path = path.resolve()
    payload = path.read_bytes()
    manifest = json.loads(payload)
    if not isinstance(manifest, dict) or manifest.get("format") != _REVIEW_FORMAT:
        raise ValueError("review manifest format is unsupported")
    if manifest.get("status") != "REVIEW_REQUIRED" or manifest.get("activation") != "not-activated":
        raise ValueError("review manifest must remain inactive and review-required")
    if not isinstance(manifest.get("items"), list):
        raise ValueError("review manifest items are invalid")
    return manifest, _sha256_bytes(payload)


def _confirmed_candidates(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """Return validated approval-queue entries without trusting path inference."""

    result: list[dict[str, Any]] = []
    for item in manifest["items"]:
        if not isinstance(item, dict):
            raise ValueError("review manifest contains an invalid item")
        if item.get("decision") != _CONFIRMED_DECISION:
            continue
        verification = _require_dict(item.get("candidate_verification"), label="candidate_verification")
        candidates = item.get("candidates")
        if (
            verification.get("status") != _CONFIRMED_STATUS
            or verification.get("candidate_count") != 1
            or verification.get("content_digest_confirmed_candidate_count") != 1
            or not isinstance(candidates, list)
            or len(candidates) != 1
        ):
            raise ValueError(f"{item.get('review_id')}: confirmed candidate evidence is inconsistent")
        candidate = _require_dict(candidates[0], label="candidate")
        path = Path(str(candidate.get("path", ""))).expanduser().absolute()
        digest = str(candidate.get("sha256") or "")
        if not _DIGEST.fullmatch(digest):
            raise ValueError(f"{item.get('review_id')}: candidate digest is invalid")
        cursor = path.parent
        while True:
            if cursor.is_symlink():
                raise ValueError(f"{item.get('review_id')}: candidate path contains a symlink")
            if cursor.parent == cursor:
                break
            cursor = cursor.parent
        if path.is_symlink():
            raise ValueError(f"{item.get('review_id')}: candidate path contains a symlink")
        if not path.is_file() or _sha256(path) != digest:
            raise ValueError(f"{item.get('review_id')}: candidate file is missing or changed")
        result.append({"review_id": str(item.get("review_id") or ""), "digest": digest, "path": path})
    return result


def _portable_result(result: Any) -> bool:
    """Check the result surface, while deliberately excluding returned bytes."""

    if result.status != "ok" or not isinstance(result.data, Mapping):
        return False
    expected_keys = {
        "resource_uri",
        "start",
        "end",
        "content_digest",
        "asset_digest",
        "content_base64",
        "mime_type",
        "truncated",
        "next_locator",
    }
    if set(result.data) != expected_keys:
        return False
    portable_fields = [
        result.data.get("resource_uri"),
        result.data.get("content_digest"),
        result.data.get("asset_digest"),
        result.data.get("mime_type"),
    ]
    if any(not isinstance(value, str) or _PORTABLE_PATH.search(value) for value in portable_fields):
        return False
    if not result.evidence or any(
        not isinstance(evidence.resource_uri, str) or _PORTABLE_PATH.search(evidence.resource_uri)
        for evidence in result.evidence
    ):
        return False
    return True


async def _verify_assets(
    *,
    repository: SqliteCatalogQueryRepository,
    candidate_entries: list[dict[str, Any]],
    space_id: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    by_digest: dict[str, Path] = {}
    for entry in candidate_entries:
        # Equal bytes are safe to share as one explicit host binding.  The
        # review entry itself remains separate, so duplicate legacy references
        # are still counted in the report.
        by_digest.setdefault(entry["digest"], entry["path"])

    assets = [
        dict(asset)
        for asset in repository.list_assets(space_id=space_id)
        if str(asset.get("content_digest") or "") in by_digest
    ]
    asset_paths = {str(asset["id"]): by_digest[str(asset["content_digest"])] for asset in assets}
    service = AssetReadService(
        catalog=repository,
        reader=LocalFilesystemBlobReader(asset_paths),
    )
    principal = Principal(
        "phase9-local-shadow",
        scopes=("knowledge.read", f"knowledge.space:{space_id}"),
    )
    results: list[dict[str, Any]] = []
    for asset in sorted(assets, key=lambda value: str(value.get("id") or "")):
        asset_id = str(asset.get("id") or "")
        uri = str(asset.get("source_uri") or "")
        digest = str(asset.get("content_digest") or "")
        path = asset_paths[asset_id]
        end = min(4096, path.stat().st_size)
        read_result = await service.read(
            principal=principal,
            correlation=Correlation("phase9_local_asset_read"),
            resource_uri=uri,
            start=0,
            end=end,
            expected_digest=digest,
        )
        bounded_digest_verified = False
        if read_result.status == "ok":
            try:
                bounded_bytes = base64.b64decode(read_result.data.get("content_base64", ""), validate=True)
                bounded_digest_verified = read_result.data.get("content_digest") == _sha256_bytes(bounded_bytes)
            except (TypeError, ValueError):
                bounded_digest_verified = False
        ok = (
            read_result.status == "ok"
            and _portable_result(read_result)
            and read_result.data.get("asset_digest") == digest
            and bounded_digest_verified
        )
        # The bounded content digest is intentionally not compared to the
        # full Asset digest; AssetReadService has already verified that full
        # digest through BlobReadResult.asset_digest.
        results.append(
            {
                "asset_id": asset_id,
                "resource_uri": uri,
                "candidate_digest": digest,
                "read_bytes": end,
                "status": "verified" if ok else "failed",
                "evidence_uri_verified": bool(read_result.evidence)
                and read_result.evidence[0].resource_uri == uri,
                "portable_result_verified": _portable_result(read_result),
                "error_code": str(read_result.error.code) if read_result.error is not None else None,
            }
        )

    summary = {
        "unique_candidate_digest_count": len(by_digest),
        "catalog_asset_match_count": len(assets),
        "verified_asset_count": sum(item["status"] == "verified" for item in results),
        "failed_asset_count": sum(item["status"] == "failed" for item in results),
    }
    return results, summary


def build_shadow_report(
    *, review_manifest_path: Path, catalog_path: Path, output_path: Path, space_id: str
) -> dict[str, Any]:
    manifest, manifest_sha256 = _load_review_manifest(review_manifest_path)
    catalog_path = catalog_path.expanduser().resolve()
    output_path = output_path.expanduser().resolve()
    if output_path == catalog_path:
        raise ValueError("shadow report must not overwrite the Catalog")
    repository = SqliteCatalogQueryRepository(catalog_path)
    revision_before = repository.catalog_revision
    candidate_entries = _confirmed_candidates(manifest)
    results, verification_summary = asyncio.run(
        _verify_assets(repository=repository, candidate_entries=candidate_entries, space_id=space_id)
    )
    revision_after = repository.catalog_revision
    if revision_before != revision_after:
        raise ValueError("Catalog changed during local Asset binding shadow")

    digest_to_assets: dict[str, list[str]] = {}
    for item in results:
        digest_to_assets.setdefault(item["candidate_digest"], []).append(item["asset_id"])
    item_results = [
        {
            "review_id": entry["review_id"],
            "candidate_digest": entry["digest"],
            "asset_ids": sorted(digest_to_assets.get(entry["digest"], [])),
            "status": "verified"
            if digest_to_assets.get(entry["digest"])
            and all(item["status"] == "verified" for item in results if item["candidate_digest"] == entry["digest"])
            else "failed",
        }
        for entry in sorted(candidate_entries, key=lambda value: value["review_id"])
    ]
    verified_items = sum(item["status"] == "verified" for item in item_results)
    failed_items = len(item_results) - verified_items
    catalog_unchanged = revision_before == revision_after
    status = (
        "PHASE9_LOCAL_ASSET_REBIND_SHADOW_PASS_NOT_ACTIVATABLE"
        if catalog_unchanged and failed_items == 0 and verification_summary["failed_asset_count"] == 0
        else "PHASE9_LOCAL_ASSET_REBIND_SHADOW_FAILED"
    )
    report = {
        "format": _SHADOW_FORMAT,
        "status": status,
        "activation": "not-activated",
        "mode": "explicit-host-binding-read-only",
        "scope": "local development staged Catalog; human approval still required; no restore/rebind executor",
        "review_manifest": {"sha256": manifest_sha256, "status": manifest["status"]},
        "catalog": {
            "catalog_revision_before": revision_before,
            "catalog_revision_after": revision_after,
            "unchanged": catalog_unchanged,
        },
        "summary": {
            "review_item_count": len(manifest["items"]),
            "confirmed_candidate_item_count": len(candidate_entries),
            "skipped_ambiguous_or_unresolved_item_count": len(manifest["items"]) - len(candidate_entries),
            "verified_item_count": verified_items,
            "failed_item_count": failed_items,
            **verification_summary,
            "policy": (
                "content digest proves byte equality only; ownership, permissions, file type, and persistent binding "
                "remain human approval requirements"
            ),
        },
        "items": item_results,
        "assets": results,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--review-manifest",
        type=Path,
        default=Path("artifacts/phase0b-local-catalog/restore-rebind-review.json"),
    )
    parser.add_argument(
        "--catalog",
        type=Path,
        default=Path("artifacts/phase0b-local-catalog/knowledge-platform.sqlite3"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/phase0b-local-catalog/local-asset-rebind-shadow-report.json"),
    )
    parser.add_argument("--space-id", default="space_kb_default")
    args = parser.parse_args()
    report = build_shadow_report(
        review_manifest_path=args.review_manifest,
        catalog_path=args.catalog,
        output_path=args.output,
        space_id=args.space_id,
    )
    print(json.dumps({"status": report["status"], "summary": report["summary"], "report": str(args.output)}, ensure_ascii=False))
    return 0 if report["status"].endswith("NOT_ACTIVATABLE") else 1


if __name__ == "__main__":
    raise SystemExit(main())
