"""Build a path-free queue for explicit local Asset binding approval.

The queue is evidence for a human decision, not an approval and not a
binding manifest.  It reports only review IDs, content digests, byte counts,
and staged Catalog Asset IDs.  Candidate paths and file contents never enter
the output.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from knowledge_platform.catalog.sqlite_query import SqliteCatalogQueryRepository
from scripts.phase9_local_asset_binding_prepare import _review_candidate_options
from scripts.phase9_local_asset_rebind_shadow import _load_review_manifest

_FORMAT = "agent-knowledge-platform-local-asset-binding-review-queue/v1"
_NON_PORTABLE_LABEL = re.compile(
    r"(?i)(?:file://|https?://|/(?:Users|private|tmp|var|home|etc|opt|usr|root|Volumes)/|[A-Za-z]:[\\/]|\\\\)"
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _portable_label(value: object) -> str:
    label = str(value or "").replace("\n", " ").replace("\r", " ").strip()
    if not label or len(label) > 240 or _NON_PORTABLE_LABEL.search(label):
        return "<redacted-label>"
    return label


def build_review_queue(
    *, review_manifest_path: Path, catalog_path: Path, output_path: Path, space_id: str = "space_kb_default"
) -> dict[str, Any]:
    review_manifest_path = review_manifest_path.expanduser().absolute()
    catalog_path = catalog_path.expanduser().absolute()
    output_path = output_path.expanduser().absolute()
    manifest, manifest_sha256 = _load_review_manifest(review_manifest_path)
    candidate_options = _review_candidate_options(manifest)
    catalog_sha256_before = _sha256(catalog_path)
    repository = SqliteCatalogQueryRepository(catalog_path)
    catalog_revision_before = repository.catalog_revision
    assets_by_digest: dict[str, list[str]] = {}
    asset_metadata: dict[str, dict[str, str]] = {}
    for asset in repository.list_assets(space_id=space_id):
        digest = str(asset.get("content_digest") or "")
        asset_id = str(asset.get("id") or "")
        if digest and asset_id:
            assets_by_digest.setdefault(digest, []).append(asset_id)
            asset_metadata[asset_id] = {
                "asset_id": asset_id,
                "kind": _portable_label(asset.get("kind")),
                "title": _portable_label(asset.get("title")),
                "mime_type": _portable_label(asset.get("mime_type")),
            }

    queue = []
    for review_id in sorted(candidate_options):
        options = candidate_options[review_id]
        digest = str(options[0]["digest"])
        asset_ids = sorted(assets_by_digest.get(digest, []))
        queue.append(
            {
                "review_id": review_id,
                "content_digest": digest,
                "bytes": options[0]["path"].stat().st_size,
                "candidate_count": len(options),
                "candidate_selection_required": len(options) > 1,
                "catalog_asset_ids": asset_ids,
                "catalog_assets": [asset_metadata[asset_id] for asset_id in asset_ids],
                "catalog_asset_match_count": len(asset_ids),
                "approval_required": True,
            }
        )
    catalog_sha256_after = _sha256(catalog_path)
    catalog_revision_after = repository.catalog_revision
    if catalog_sha256_before != catalog_sha256_after or catalog_revision_before != catalog_revision_after:
        raise ValueError("Catalog changed while building the review queue")

    report: dict[str, Any] = {
        "format": _FORMAT,
        "status": "PHASE9_LOCAL_ASSET_BINDING_REVIEW_QUEUE_READY_NOT_ACTIVATABLE",
        "activation": "not-activated",
        "execution_allowed": False,
        "review_manifest": {"sha256": manifest_sha256, "status": manifest["status"]},
        "catalog": {
            "revision": catalog_revision_before,
            "space_id": space_id,
            "canonical_sha256_before": catalog_sha256_before,
            "canonical_sha256_after": catalog_sha256_after,
            "canonical_unchanged": catalog_sha256_before == catalog_sha256_after,
        },
        "summary": {
            "review_item_count": len(manifest["items"]),
            "confirmed_candidate_item_count": len(queue),
            "ambiguous_candidate_item_count": sum(item["candidate_selection_required"] for item in queue),
            "unique_candidate_digest_count": len({entry["content_digest"] for entry in queue}),
            "matched_candidate_item_count": sum(item["catalog_asset_match_count"] > 0 for item in queue),
            "matched_catalog_asset_count": len(
                {asset_id for item in queue for asset_id in item["catalog_asset_ids"]}
            ),
            "approval_required": True,
        },
        "policy": "This is a review queue only; an explicit human approval is required before a Host binding manifest can be prepared.",
        "items": queue,
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
        default=Path("artifacts/phase0b-local-catalog/local-asset-binding-approval-queue.json"),
    )
    parser.add_argument("--space-id", default="space_kb_default")
    args = parser.parse_args()
    report = build_review_queue(
        review_manifest_path=args.review_manifest,
        catalog_path=args.catalog,
        output_path=args.output,
        space_id=args.space_id,
    )
    print(json.dumps({"status": report["status"], "summary": report["summary"], "output": str(args.output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
