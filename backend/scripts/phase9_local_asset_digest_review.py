"""Discover local Asset files by exact content digest for human review.

This is a host-local review manifest, not an approval or binding executor.
Exact bytes prove content equality only; every selected review ID still needs
explicit human approval before ``phase9_local_asset_binding_prepare.py`` can
create a binding manifest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
from collections import defaultdict
from pathlib import Path
from typing import Any

from knowledge_platform.catalog import SqliteCatalogQueryRepository

_FORMAT = "agent-knowledge-platform-restore-review/v1"
_UNIQUE_DECISION = "human-review-required-content-matched-single-candidate"
_AMBIGUOUS_DECISION = "human-review-required-select-one-candidate"
_UNRESOLVED_DECISION = "unresolved-no-candidate"
_SKIP_DIRS = frozenset({".git", ".obsidian", "__pycache__"})


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _safe_root(root: Path) -> Path:
    root = root.expanduser().absolute()
    if root.is_symlink() or not root.is_dir():
        raise ValueError("scan root must be a non-symlink directory")
    return root


def _iter_regular_files(root: Path):
    for current, directories, filenames in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        directories[:] = [name for name in directories if name not in _SKIP_DIRS]
        safe_directories: list[str] = []
        for name in directories:
            path = current_path / name
            if not path.is_symlink():
                safe_directories.append(name)
        directories[:] = safe_directories
        for name in filenames:
            path = current_path / name
            if path.is_symlink():
                continue
            try:
                mode = path.stat().st_mode
            except OSError:
                continue
            if stat.S_ISREG(mode):
                yield path


def _review_id(*, asset_id: str, digest: str, root: Path) -> str:
    material = json.dumps(
        {"asset_id": asset_id, "content_digest": digest, "scan_root": str(root)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(material).hexdigest()


def build_digest_review(
    *, catalog_path: Path, root: Path, output_path: Path, space_id: str = "space_kb_default"
) -> dict[str, Any]:
    catalog_path = catalog_path.expanduser().absolute()
    root = _safe_root(root)
    output_path = output_path.expanduser().absolute()
    repository = SqliteCatalogQueryRepository(catalog_path)
    revision_before = repository.catalog_revision
    assets = [dict(asset) for asset in repository.list_assets(space_id=space_id)]
    asset_digests = {str(asset["id"]): str(asset.get("content_digest") or "") for asset in assets}
    wanted_digests = set(asset_digests.values())
    files_by_digest: dict[str, list[dict[str, Any]]] = defaultdict(list)
    scanned_files = 0
    scanned_bytes = 0
    for path in _iter_regular_files(root):
        try:
            size = path.stat().st_size
            digest = _sha256_file(path)
        except OSError:
            continue
        scanned_files += 1
        scanned_bytes += size
        if digest in wanted_digests:
            files_by_digest[digest].append({"path": str(path), "bytes": size, "sha256": digest})

    items: list[dict[str, Any]] = []
    decision_counts = {"unique": 0, "ambiguous": 0, "unresolved": 0}
    for asset in sorted(assets, key=lambda item: str(item.get("id") or "")):
        asset_id = str(asset.get("id") or "")
        digest = str(asset.get("content_digest") or "")
        candidates = sorted(files_by_digest.get(digest, []), key=lambda item: str(item["path"]))
        if len(candidates) == 1:
            decision = _UNIQUE_DECISION
            verification = {
                "candidate_count": 1,
                "content_digest_confirmed_candidate_count": 1,
                "status": "unique-content-digest-confirmed-review-required",
            }
            decision_counts["unique"] += 1
        elif candidates:
            decision = _AMBIGUOUS_DECISION
            verification = {
                "candidate_count": len(candidates),
                "content_digest_confirmed_candidate_count": len(candidates),
                "status": "ambiguous-content-digest-confirmed-review-required",
            }
            decision_counts["ambiguous"] += 1
        else:
            decision = _UNRESOLVED_DECISION
            verification = {
                "candidate_count": 0,
                "content_digest_confirmed_candidate_count": 0,
                "status": "no-content-digest-candidate",
            }
            decision_counts["unresolved"] += 1
        items.append(
            {
                "review_id": _review_id(asset_id=asset_id, digest=digest, root=root),
                "decision": decision,
                "asset_id": asset_id,
                "space_id": space_id,
                "digest": digest,
                "candidate_verification": verification,
                "candidates": candidates,
            }
        )

    revision_after = repository.catalog_revision
    if revision_before != revision_after:
        raise ValueError("Catalog changed while discovering digest candidates")
    matching_asset_count = sum(bool(files_by_digest.get(digest)) for digest in asset_digests.values())
    result = {
        "format": _FORMAT,
        "status": "REVIEW_REQUIRED",
        "activation": "not-activated",
        "execution_allowed": False,
        "scope": "host-local content-digest candidate discovery; human approval required",
        "catalog": {"revision": revision_before, "space_id": space_id, "asset_count": len(assets)},
        "scan": {
            "root_path": str(root),
            "scanned_file_count": scanned_files,
            "scanned_bytes": scanned_bytes,
            "matching_asset_count": matching_asset_count,
            "matching_file_count": sum(len(value) for value in files_by_digest.values()),
        },
        "summary": {
            "asset_count": len(assets),
            "unique_candidate_asset_count": decision_counts["unique"],
            "ambiguous_candidate_asset_count": decision_counts["ambiguous"],
            "unresolved_asset_count": decision_counts["unresolved"],
            "approval_required": True,
            "policy": "content digest proves byte equality only; no binding is approved or executed",
        },
        "items": items,
    }
    if output_path.exists() and output_path.is_symlink():
        raise ValueError("review output must not be a symlink")
    cursor = output_path.parent
    while True:
        if cursor.is_symlink():
            raise ValueError("review output parent must not be a symlink")
        if cursor.parent == cursor:
            break
        cursor = cursor.parent
    if output_path.exists() and not output_path.is_file():
        raise ValueError("review output must be a regular file")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=Path("artifacts/phase0b-local-catalog/knowledge-platform.sqlite3"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("artifacts/phase0b-local-catalog/local-asset-content-review.json"))
    parser.add_argument("--space-id", default="space_kb_default")
    args = parser.parse_args()
    result = build_digest_review(
        catalog_path=args.catalog,
        root=args.root,
        output_path=args.output,
        space_id=args.space_id,
    )
    print(json.dumps({"status": result["status"], "summary": result["summary"], "output": str(args.output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
