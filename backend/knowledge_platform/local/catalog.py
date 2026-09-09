"""Snapshot an explicit Catalog and materialize published Wiki in a private copy."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from knowledge_contracts import Principal
from knowledge_platform.catalog import CollectionFreshnessObservation, SqliteCollectionFreshnessWriter

_SPACE_ID = "space_kb_default"
_DATASET_ID = "dataset_kb_default"
_MAX_PAGES = 5000
_MAX_PAGE_BYTES = 8 * 1024 * 1024
_TITLE_RE = re.compile(r"(?ms)^---\n(?P<frontmatter>.*?)(?:\n---\n|\Z)")


def _file_digest(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return f"sha256:{digest.hexdigest()}", size

def _safe_pages(root: Path) -> list[Path]:
    root = root.expanduser().absolute()
    cursor = root
    while True:
        if cursor.is_symlink():
            raise OSError("published Wiki root contains a symlinked path component")
        if cursor.parent == cursor:
            break
        cursor = cursor.parent
    if not root.is_dir():
        raise OSError("published Wiki root must be a real directory")
    pages: list[Path] = []
    for path in sorted(root.rglob("*.md")):
        relative = path.relative_to(root).as_posix()
        if relative in {"index.md", "log.md"}:
            continue
        if path.is_symlink() or not path.is_file():
            raise OSError("published Wiki page must be a real file")
        cursor = path.parent
        while True:
            if cursor.is_symlink():
                raise OSError("published Wiki page contains a symlinked parent")
            if cursor == root:
                break
            if cursor.parent == cursor:
                raise OSError("published Wiki page escaped its root")
            cursor = cursor.parent
        if path.stat().st_size > _MAX_PAGE_BYTES:
            raise ValueError("published Wiki page exceeds the shadow size limit")
        pages.append(path)
    if not pages:
        raise ValueError("published Wiki root contains no pages")
    if len(pages) > _MAX_PAGES:
        raise ValueError("published Wiki page count exceeds the shadow limit")
    return pages

def _page_title(path: Path, fallback: str) -> str:
    content = path.read_text(encoding="utf-8")
    match = _TITLE_RE.match(content[:64 * 1024])
    if match:
        for line in match.group("frontmatter").splitlines():
            key, separator, value = line.partition(":")
            if separator and key.strip() == "title" and value.strip():
                return value.strip().strip("'\"")[:500]
    return fallback[:500]

def _snapshot_catalog(catalog_path: Path, temporary_catalog: Path) -> None:
    """Create a consistent, non-symlinked SQLite snapshot for the shadow."""

    catalog_path = catalog_path.expanduser().absolute()
    temporary_catalog = temporary_catalog.expanduser().absolute()
    cursor = catalog_path
    while True:
        if cursor.is_symlink():
            raise OSError("Catalog source contains a symlinked path component")
        if cursor.parent == cursor:
            break
        cursor = cursor.parent
    if not catalog_path.is_file():
        raise OSError("Catalog source must be a regular file")
    if temporary_catalog.exists():
        raise FileExistsError("temporary Catalog target must not already exist")
    before_stat = os.stat(catalog_path, follow_symlinks=False)
    if not stat.S_ISREG(before_stat.st_mode):
        raise OSError("Catalog source must be a regular file")
    # Use SQLite's backup API instead of copying only the main file.  It takes
    # a consistent read snapshot and includes committed pages still in WAL.
    source_uri = f"file:{quote(str(catalog_path), safe='/')}?mode=ro"
    source = sqlite3.connect(source_uri, uri=True)
    target = sqlite3.connect(temporary_catalog)
    try:
        # The connection is now anchored to the file SQLite opened.  Recheck
        # the path identity to catch a symlink swap in the check/connect gap.
        after_stat = os.stat(catalog_path, follow_symlinks=False)
        if (before_stat.st_dev, before_stat.st_ino) != (after_stat.st_dev, after_stat.st_ino):
            raise OSError("Catalog source changed while opening")
        source.backup(target)
        target.commit()
    finally:
        target.close()
        source.close()

def _materialize_catalog(catalog_path: Path, temporary_catalog: Path, wiki_root: Path) -> dict[str, Any]:
    # Normalize host-provided roots once at the boundary.  ``_safe_pages``
    # already normalizes its own input, but the relative-path form would make
    # ``page.relative_to(wiki_root)`` compare absolute pages with a relative
    # root and fail before the actual boundary checks ran.
    wiki_root = wiki_root.expanduser().absolute()
    _snapshot_catalog(catalog_path, temporary_catalog)
    pages = _safe_pages(wiki_root)
    records: list[tuple[str, Path, str, int, str]] = []
    for page in pages:
        slug = page.relative_to(wiki_root).with_suffix("").as_posix()
        content_digest, size = _file_digest(page)
        asset_id = "asset_wiki_" + hashlib.sha256(f"{_SPACE_ID}:{slug}".encode()).hexdigest()[:32]
        records.append((asset_id, page, slug, size, content_digest))

    connection = sqlite3.connect(temporary_catalog)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        dataset = connection.execute(
            "SELECT asset_ids, capabilities, version FROM knowledge_datasets WHERE id = ? AND space_id = ?",
            (_DATASET_ID, _SPACE_ID),
        ).fetchone()
        if dataset is None:
            raise ValueError("staged Catalog does not contain the expected local Collection")
        asset_ids = json.loads(dataset[0])
        capabilities = json.loads(dataset[1])
        if not isinstance(asset_ids, list) or not isinstance(capabilities, list):
            raise ValueError("staged Catalog Collection metadata is malformed")
        collection_version = str(dataset[2])
        created_at = datetime.now(UTC).isoformat()
        for asset_id, path, slug, size, content_digest in records:
            title = _page_title(path, slug.rsplit("/", 1)[-1])
            existing = connection.execute(
                "SELECT space_id, kind, source_type, source_uri, metadata_json FROM knowledge_assets WHERE id = ?",
                (asset_id,),
            ).fetchone()
            uri = f"knowledge://spaces/{_SPACE_ID}/assets/{asset_id}"
            if existing is not None:
                metadata = json.loads(existing[4])
                if (existing[:4] != (_SPACE_ID, "wiki_page", "local_published_wiki", uri)
                        or not isinstance(metadata, dict) or metadata.get("wiki_slug") != slug):
                    raise ValueError("local Wiki Asset id is owned by a different source")
            connection.execute(
                """INSERT INTO knowledge_assets
                (id, space_id, kind, title, description, mime_type, source_type,
                 source_uri, revision, content_digest, permissions_json,
                 metadata_json, created_at, updated_at)
                VALUES (?, ?, 'wiki_page', ?, '', 'text/markdown',
                        'local_published_wiki', ?, ?, ?, '{}', ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET title=excluded.title, revision=excluded.revision,
                    content_digest=excluded.content_digest, metadata_json=excluded.metadata_json,
                    updated_at=excluded.updated_at""",
                (
                    asset_id,
                    _SPACE_ID,
                    title,
                    f"knowledge://spaces/{_SPACE_ID}/assets/{asset_id}",
                    content_digest,
                    content_digest,
                    json.dumps({"published": True, "wiki_slug": slug, "bytes": size}, ensure_ascii=False),
                    created_at,
                    created_at,
                ),
            )
            if asset_id not in asset_ids:
                asset_ids.append(asset_id)
        if "wiki_query" not in capabilities:
            capabilities.append("wiki_query")
        connection.execute(
            "UPDATE knowledge_datasets SET asset_ids = ?, capabilities = ? WHERE id = ? AND space_id = ?",
            (
                json.dumps(asset_ids, ensure_ascii=False),
                json.dumps(capabilities, ensure_ascii=False),
                _DATASET_ID,
                _SPACE_ID,
            ),
        )
        connection.commit()
    finally:
        connection.close()
    SqliteCollectionFreshnessWriter(temporary_catalog).observe(
        principal=Principal(
            subject_id="phase6-local-wiki-shadow",
            scopes=("knowledge.processing", f"knowledge.space:{_SPACE_ID}"),
        ),
        observation=CollectionFreshnessObservation(
            collection_id=_DATASET_ID,
            collection_version=collection_version,
            space_id=_SPACE_ID,
            capability="wiki_query",
            state="ready",
            mode="local_published_wiki",
            observed_at=created_at,
            source_revision="local-published-wiki",
        ),
    )
    return {
        "pages": len(records),
        "collection_version": collection_version,
        "asset_ids": [item[0] for item in records],
        "page_digests": {item[0]: item[4] for item in records},
        "file_bindings": {item[0]: item[1] for item in records},
    }
