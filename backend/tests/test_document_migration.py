from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from knowledge_platform.distribution.document_migration import prepare_document_migration


def _legacy_catalog(path: Path, *, content_digest: str) -> None:
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE knowledge_bases (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT,
            created_at DATETIME, updated_at DATETIME
        );
        CREATE TABLE knowledge_documents (
            id TEXT PRIMARY KEY, knowledge_base_id TEXT NOT NULL,
            title TEXT NOT NULL, source_type TEXT, source_path TEXT,
            storage_path TEXT, virtual_path TEXT, mime_type TEXT,
            content_sha256 TEXT, status TEXT, doc_metadata JSON,
            origin_url TEXT, created_at DATETIME, updated_at DATETIME
        );
        INSERT INTO knowledge_bases VALUES
          ('kb-1', 'Docs', 'migrated', '2026-09-11 00:00:00.000000', '2026-09-11 00:00:00.000000');
        """
    )
    db.execute(
        """INSERT INTO knowledge_documents
        (id, knowledge_base_id, title, source_type, source_path, storage_path,
         virtual_path, mime_type, content_sha256, status, doc_metadata,
         origin_url, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "doc-1", "kb-1", "Read me", "local", "docs/readme.md",
            "docs/readme.md", "readme.md", "text/markdown", content_digest,
            "published", "{}", "", "2026-09-11 00:00:00.000000", "2026-09-11 00:00:00.000000",
        ),
    )
    db.commit()
    db.close()


def _run(tmp_path: Path, *, digest: str | None = None, bindings: dict[str, str] | None = None):
    catalog = tmp_path / "legacy.sqlite3"
    files = tmp_path / "files"
    files.mkdir()
    body = b"# Migrated document\n\nportable content\n"
    relative = Path("docs/readme.md")
    (files / relative).parent.mkdir()
    (files / relative).write_bytes(body)
    digest = digest or hashlib.sha256(body).hexdigest()
    _legacy_catalog(catalog, content_digest=digest)
    output = tmp_path / "package"
    result = prepare_document_migration(
        catalog,
        files,
        bindings if bindings is not None else {"doc-1": relative.as_posix()},
        output,
        installation_id="install-1",
        source_revision="legacy-1",
    )
    return result, catalog, files, output, body


def _target_catalog(output: Path) -> Path:
    candidate = output / "catalog.sqlite3"
    assert candidate.is_file(), f"migration did not publish a target Catalog: {sorted(output.iterdir())}"
    return candidate


def _blob(output: Path, digest: str) -> bytes:
    hex_digest = digest.removeprefix("sha256:")
    candidates = (output / "blobs" / hex_digest, output / "blobs" / "sha256" / hex_digest)
    for candidate in candidates:
        if candidate.is_file():
            return candidate.read_bytes()
    raise AssertionError(f"blob {hex_digest} was not materialized")


def test_real_legacy_document_is_materialized_and_readable(tmp_path: Path) -> None:
    result, catalog, _files, output, body = _run(tmp_path)
    assert result["state"] == "verified_inactive"
    assert result["activation_allowed"] is False
    assert catalog.is_file()

    target = _target_catalog(output)
    with sqlite3.connect(target) as db:
        db.row_factory = sqlite3.Row
        asset = db.execute(
            "SELECT id, space_id, title, source_uri, content_digest FROM knowledge_assets"
        ).fetchone()
        collection = db.execute("SELECT id, space_id, asset_ids FROM knowledge_datasets").fetchone()
    assert asset is not None
    assert asset["title"] == "Read me"
    assert asset["source_uri"] == f"knowledge://spaces/{asset['space_id']}/assets/{asset['id']}"
    assert json.loads(collection["asset_ids"]) == [asset["id"]]
    assert _blob(output, asset["content_digest"]) == body
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["activation_allowed"] is False
    assert manifest["asset_bindings"][asset["id"]].startswith("blobs/")


def test_digest_mismatch_is_rejected_without_publishing(tmp_path: Path) -> None:
    catalog = tmp_path / "legacy.sqlite3"
    files = tmp_path / "files"
    files.mkdir()
    (files / "doc.md").write_bytes(b"actual")
    _legacy_catalog(catalog, content_digest="0" * 64)
    output = tmp_path / "package"
    with pytest.raises((ValueError, RuntimeError, OSError)):
        prepare_document_migration(catalog, files, {"doc-1": "doc.md"}, output)
    assert not output.exists()


@pytest.mark.parametrize("binding", ["../outside.md", "/etc/passwd", "missing.md"])
def test_binding_must_be_present_and_confined_to_source_root(tmp_path: Path, binding: str) -> None:
    catalog = tmp_path / "legacy.sqlite3"
    files = tmp_path / "files"
    files.mkdir()
    (files / "doc.md").write_bytes(b"actual")
    digest = hashlib.sha256(b"actual").hexdigest()
    _legacy_catalog(catalog, content_digest=digest)
    output = tmp_path / "package"
    with pytest.raises((ValueError, RuntimeError, OSError)):
        prepare_document_migration(catalog, files, {"doc-1": binding}, output)
    assert not output.exists()


def test_missing_document_binding_is_rejected(tmp_path: Path) -> None:
    catalog = tmp_path / "legacy.sqlite3"
    files = tmp_path / "files"
    files.mkdir()
    _legacy_catalog(catalog, content_digest=hashlib.sha256(b"actual").hexdigest())
    output = tmp_path / "package"
    with pytest.raises((ValueError, RuntimeError)):
        prepare_document_migration(catalog, files, {}, output)
    assert not output.exists()
