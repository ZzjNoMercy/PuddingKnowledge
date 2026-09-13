"""Atomic, inactive workspace containing one document Catalog and one Wiki archive."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
from urllib.parse import quote
from knowledge_platform.distribution import wiki_archive as archive_verifier
from . import migrated_documents as documents_verifier

from .migrated_documents import (
    MigratedDocumentWorkspaceError,
    bootstrap_migrated_documents,
    load_migrated_workspace,
)
from .migrated_wiki import (
    MigratedWikiWorkspaceError,
    bootstrap_migrated_wiki,
    load_migrated_wiki_workspace,
)


class CombinedWorkspaceError(RuntimeError):
    pass


_TABLES = ("knowledge_spaces", "knowledge_assets", "knowledge_datasets")
_SCHEMA_TABLE = "knowledge_catalog_schema_versions"
_MANIFEST_KEYS = {"version", "owner", "kind", "catalog", "blob_root", "evidence_root",
                  "document_manifest", "wiki_manifest", "activation_allowed"}


def _private(path: Path | str) -> Path:
    path = Path(path).expanduser()
    if not path.is_absolute() or ".." in path.parts:
        raise CombinedWorkspaceError("workspace paths must be absolute without parent traversal")
    cursor = path
    while True:
        if cursor.is_symlink():
            raise CombinedWorkspaceError("workspace path contains a symlink")
        if cursor.parent == cursor:
            break
        cursor = cursor.parent
    return path


def _write(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _sync(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _schema(db: sqlite3.Connection) -> dict[str, tuple]:
    names = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    return {name: tuple(tuple(row) for row in db.execute(f'PRAGMA table_info("{name}")')) for name in names}


def _objects(db: sqlite3.Connection) -> list[tuple]:
    return list(db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name,tbl_name,sql"))


def _merge_catalog(document: Path, wiki: Path, target: Path) -> None:
    shutil.copyfile(document, target)
    with sqlite3.connect(target) as out, sqlite3.connect("file:" + quote(str(wiki), safe="/") + "?mode=ro&immutable=1", uri=True) as source:
        out.execute("PRAGMA foreign_keys=ON")
        if _schema(out) != _schema(source) or _objects(out) != _objects(source):
            raise CombinedWorkspaceError("document and Wiki Catalog schemas differ")
        allowed = set(_TABLES) | {_SCHEMA_TABLE}
        for table in _schema(source):
            if table not in allowed and source.execute(f'SELECT 1 FROM "{table}" LIMIT 1').fetchone() is not None:
                raise CombinedWorkspaceError(f"Wiki Catalog contains unsupported rows in {table}")
        versions = lambda db: [r[0] for r in db.execute(f'SELECT version FROM "{_SCHEMA_TABLE}" ORDER BY version')]
        if versions(out) != versions(source):
            raise CombinedWorkspaceError("Catalog schema versions differ")
        # The schema version table describes shared metadata and is never copied.
        for table in _TABLES:
            columns = [r[1] for r in source.execute(f'PRAGMA table_info("{table}")')]
            rows = source.execute(f'SELECT * FROM "{table}"').fetchall()
            if not rows:
                continue
            placeholders = ",".join("?" for _ in columns)
            quoted = ",".join('"' + c.replace('"', '""') + '"' for c in columns)
            try:
                out.executemany(f'INSERT INTO "{table}" ({quoted}) VALUES ({placeholders})', rows)
            except sqlite3.IntegrityError as error:
                raise CombinedWorkspaceError("Wiki Catalog identity collision") from error
        out.commit()
    _sync(target.parent)


def _source_commitments(document, wiki):
    with documents_verifier._candidate_lock(document):
        raw = documents_verifier._read(document / "manifest.json", 1024 * 1024)
        manifest = documents_verifier._json(raw)
        bindings = documents_verifier._bindings(manifest.get("asset_bindings"))
        files = manifest.get("files")
        if not isinstance(files, dict) or set(files) != {"catalog.sqlite3", *bindings.values()}:
            raise CombinedWorkspaceError("document candidate inventory is invalid")
        total = 0
        for relative, expected in files.items():
            data = documents_verifier._read(document / relative, 64 * 1024 * 1024)
            total += len(data)
            if total > 256 * 1024 * 1024 or documents_verifier._digest(data) != expected:
                raise CombinedWorkspaceError("document candidate changed")
        document_digest = documents_verifier._digest(raw)
        document_identity = (document.stat().st_dev, document.stat().st_ino)
    with archive_verifier._lock(wiki, shared=True):
        archive_verifier._verify(wiki)
        wiki_digest = documents_verifier._digest(archive_verifier._read(wiki / "manifest.json", private=True, limit=archive_verifier.MAX_JSON)[0])
        wiki_identity = (wiki.stat().st_dev, wiki.stat().st_ino)
    return document_digest, document_identity, wiki_digest, wiki_identity


def bootstrap_combined_workspace(document_candidate: Path | str, wiki_archive: Path | str, state_dir: Path | str, *, schema_evidence: Path | None = None) -> dict:
    document_candidate, wiki_archive, root = _private(document_candidate), _private(wiki_archive), _private(state_dir)
    if root == document_candidate or root == wiki_archive or root.is_relative_to(document_candidate) or root.is_relative_to(wiki_archive) or document_candidate.is_relative_to(root) or wiki_archive.is_relative_to(root):
        raise CombinedWorkspaceError("inputs and owned state must be disjoint")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.stat().st_mode & 0o077 or root.stat().st_uid != os.getuid():
        raise CombinedWorkspaceError("workspace root must be owned and private")
    if any(p.name != ".workspace.lock" for p in root.iterdir()):
        raise CombinedWorkspaceError("workspace must be empty")
    source_commitments = _source_commitments(document_candidate, wiki_archive)
    marker = root / ".initializing"
    _write(marker, b"combined workspace bootstrap\n")
    _sync(root)
    staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=root))
    try:
        doc_stage, wiki_stage = staging / "doc", staging / "wiki"
        bootstrap_migrated_documents(document_candidate, doc_stage)
        bootstrap_migrated_wiki(wiki_archive, wiki_stage, schema_evidence=schema_evidence)
        _merge_catalog(doc_stage / "catalog.sqlite3", wiki_stage / "catalog.sqlite3", staging / "catalog.sqlite3")
        os.chmod(staging / "catalog.sqlite3", 0o600)
        os.replace(doc_stage / "blobs", staging / "blobs")
        os.replace(wiki_stage / "wiki-evidence", staging / "wiki-evidence")
        doc_manifest = json.loads((doc_stage / "workspace.json").read_bytes())
        wiki_manifest = json.loads((wiki_stage / "workspace.json").read_bytes())
        manifest = {"version": 4, "owner": "puddingknowledge-local", "kind": "migrated_knowledge",
                    "catalog": "catalog.sqlite3", "blob_root": "blobs", "evidence_root": "wiki-evidence",
                    "document_manifest": doc_manifest, "wiki_manifest": wiki_manifest, "activation_allowed": False}
        _write(staging / "workspace.json", json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode())
        for name in ("catalog.sqlite3", "blobs", "wiki-evidence", "workspace.json"):
            os.replace(staging / name, root / name)
        if schema_evidence is not None:
            os.replace(wiki_stage / "wiki-schema.json", root / "wiki-schema.json")
        _sync(root)
        shutil.rmtree(staging)
        loaded = load_combined_workspace(root, manifest, _initializing_ok=True)
        if _source_commitments(document_candidate, wiki_archive) != source_commitments:
            raise CombinedWorkspaceError("migration inputs changed during combined bootstrap")
        marker.unlink()
        _sync(root)
        return loaded
    except (MigratedDocumentWorkspaceError, MigratedWikiWorkspaceError, CombinedWorkspaceError, OSError, sqlite3.Error) as error:
        raise CombinedWorkspaceError(str(error)) from error
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def load_combined_workspace(root: Path | str, manifest: dict, *, _initializing_ok: bool = False) -> dict:
    root = _private(root)
    if not root.is_dir() or root.stat().st_mode & 0o077 or root.stat().st_uid != os.getuid():
        raise CombinedWorkspaceError("workspace root must be private")
    if set(manifest) != _MANIFEST_KEYS or type(manifest.get("version")) is not int or manifest.get("version") != 4 or manifest.get("owner") != "puddingknowledge-local" or manifest.get("kind") != "migrated_knowledge" or manifest.get("catalog") != "catalog.sqlite3" or manifest.get("blob_root") != "blobs" or manifest.get("evidence_root") != "wiki-evidence" or manifest.get("activation_allowed") is not False:
        raise CombinedWorkspaceError("combined workspace manifest is invalid")
    if (root / ".initializing").exists() or (root / ".initializing").is_symlink():
        if _initializing_ok:
            pass
        else:
            raise CombinedWorkspaceError("state-dir contains an incomplete initialization")
    for name in ("catalog.sqlite3", "blobs", "wiki-evidence"):
        if not (root / name).exists():
            raise CombinedWorkspaceError("combined workspace is incomplete")
    dm, wm = manifest["document_manifest"], manifest["wiki_manifest"]
    if not isinstance(dm, dict) or not isinstance(wm, dict):
        raise CombinedWorkspaceError("nested workspace manifests are invalid")
    if any(value.get("catalog") != "catalog.sqlite3" or value.get("owner") != "puddingknowledge-local" or type(value.get("version")) is not int or value["version"] not in versions for value, versions in ((dm, (2,)), (wm, (3, 5, 6, 7)))):
        raise CombinedWorkspaceError("nested Catalog binding is invalid")
    try:
        documents = load_migrated_workspace(root, dm)
        wiki = load_migrated_wiki_workspace(root, wm, _combined=True, _initializing_ok=_initializing_ok)
    except Exception as error:
        raise CombinedWorkspaceError(str(error)) from error
    if set(documents["file_bindings"]) & set(wiki["file_bindings"]):
        raise CombinedWorkspaceError("document and Wiki bindings overlap")
    return {"schema_bundle": wiki["schema_bundle"], "catalog": root / "catalog.sqlite3", "file_bindings": {**documents["file_bindings"], **wiki["file_bindings"]},
            "document_bindings": documents["document_bindings"], "space_ids": sorted(set(documents["space_ids"]) | set(wiki["space_ids"])),
            "pages": wiki["pages"], "wiki_bindings": wiki["wiki_bindings"], "raw_bindings": wiki["raw_bindings"], "activation_allowed": False}
