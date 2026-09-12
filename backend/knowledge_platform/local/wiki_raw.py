"""Pure projection of verified Wiki Raw snapshots into Catalog-shaped facts."""
from __future__ import annotations

import hashlib
from pathlib import Path
import re
from typing import Any

from knowledge_platform.distribution import wiki_archive


_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_PORTABLE_STRING_FIELDS = {
    "source_id": "legacy_source_id",
    "asset_id": "legacy_asset_id",
    "created_at": "created_at",
    "bundle_hash": "bundle_hash",
}


def _fail(message: str) -> None:
    raise ValueError(message)


def _read_manifest(archive_root: Path) -> list[dict[str, Any]]:
    data, _ = wiki_archive._read(
        archive_root / "raw/manifest.jsonl", limit=wiki_archive.MAX_JSON, private=True
    )
    try:
        lines = data.decode("utf-8").splitlines()
    except UnicodeDecodeError as error:
        raise ValueError("Raw manifest is not UTF-8") from error
    records: list[dict[str, Any]] = []
    if len(lines) > wiki_archive.MAX_FILES:
        _fail("Raw manifest exceeds file budget")
    for line in lines:
        if not line.strip():
            continue
        row = wiki_archive._json(line.encode("utf-8"))
        if not isinstance(row, dict):
            _fail("Invalid Raw record")
        records.append(row)
    return records


def project_raw_assets(
    evidence_root: Path, archive_manifest: dict, space_id: str
) -> dict[str, dict[str, Any]]:
    """Project verified archived Raw records without importing or writing Catalog state.

    ``evidence_root`` is the evidence directory containing the archived ``archive``
    directory.  Every record is checked against both the verified archive inventory
    and the supplied archive manifest before it can produce a projection.
    """
    if not isinstance(evidence_root, Path):
        evidence_root = Path(evidence_root)
    if not isinstance(space_id, str) or not wiki_archive.TOKEN.fullmatch(space_id):
        _fail("Invalid space id")
    if not isinstance(archive_manifest, dict):
        _fail("Archive manifest must be an object")
    files = archive_manifest.get("files")
    if not isinstance(files, dict):
        _fail("Archive manifest inventory is invalid")

    archive_root = evidence_root / "archive"
    inventory = wiki_archive._inventory(archive_root, private=True)
    if files != inventory["files"]:
        _fail("Archive manifest inventory changed")
    # _raw performs the canonical relative-path, duplicate-path, digest, size,
    # and per-file inventory checks.  Keep it as the single archive authority.
    wiki_archive._raw(archive_root, inventory)
    records = _read_manifest(archive_root)

    assets: dict[str, dict[str, Any]] = {}
    bindings: dict[str, str] = {}
    titles: dict[str, str] = {}
    for row in records:
        snapshot_path = row.get("snapshot_path")
        if not isinstance(snapshot_path, str):
            _fail("Invalid Raw snapshot path")
        # The manifest itself is control metadata, never a user snapshot.
        if snapshot_path == "manifest.jsonl":
            _fail("Raw snapshot collides with manifest")
        fact = inventory["files"].get("raw/" + snapshot_path)
        if fact is None or files.get("raw/" + snapshot_path) != fact:
            _fail("Unknown Raw snapshot path")
        digest = row.get("sha256")
        size = row.get("size_bytes")
        if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
            _fail("Invalid Raw digest")
        if type(size) is not int or size < 0:
            _fail("Invalid Raw size")
        if fact != {"sha256": digest, "size_bytes": size}:
            _fail("Raw snapshot integrity mismatch")

        for source_key in _PORTABLE_STRING_FIELDS:
            if source_key in row and not isinstance(row[source_key], str):
                _fail("Invalid portable Raw metadata type")
        asset_id = "asset_raw_" + hashlib.sha256(
            f"{space_id}:{snapshot_path}".encode("utf-8")
        ).hexdigest()[:32]
        if asset_id in assets:
            _fail("Raw Asset identity collision")
        revision = "sha256:" + digest
        metadata: dict[str, Any] = {"snapshot_path": snapshot_path, "bytes": size}
        for source_key, target_key in _PORTABLE_STRING_FIELDS.items():
            if source_key in row:
                metadata[target_key] = row[source_key]
        assets[asset_id] = {
            "space_id": space_id,
            "kind": "raw_snapshot",
            "source_type": "local_wiki_raw",
            "source_uri": f"knowledge://spaces/{space_id}/assets/{asset_id}",
            "revision": revision,
            "content_digest": revision,
            "metadata": metadata,
        }
        bindings[asset_id] = "wiki-evidence/archive/raw/" + snapshot_path
        title = row.get("title", Path(snapshot_path).name)
        if not isinstance(title, str):
            _fail("Invalid Raw title")
        titles[asset_id] = title.strip() or Path(snapshot_path).name
    return {"assets": assets, "file_bindings": bindings, "titles": titles}
