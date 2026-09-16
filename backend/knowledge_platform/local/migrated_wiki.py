"""Durable, inactive preservation of a verified Wiki archive."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import tempfile
from urllib.parse import quote
from datetime import datetime, timezone

from sqlalchemy import create_engine, text

from knowledge_platform.catalog import migrate_to_latest
from knowledge_platform.distribution import wiki_archive
from .catalog import _materialize_catalog
from .wiki_raw import project_raw_assets
from .wiki_lineage import project_wiki_lineage


class MigratedWikiWorkspaceError(RuntimeError):
    pass


_PROVIDER = "knowledge_local_published_wiki"
_MAX_MANIFEST = 32 * 1024 * 1024
_MAX_CATALOG = 256 * 1024 * 1024


def _real(value: Path | str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute() or ".." in path.parts:
        raise MigratedWikiWorkspaceError("workspace paths must be absolute and normalized")
    cursor = path
    while True:
        if cursor.is_symlink():
            raise MigratedWikiWorkspaceError("workspace path contains a symlink")
        if cursor.parent == cursor:
            break
        cursor = cursor.parent
    return path


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _read(path: Path, limit: int) -> bytes:
    try:
        return wiki_archive._read(_real(path), limit=limit, private=True)[0]
    except (ValueError, OSError) as error:
        raise MigratedWikiWorkspaceError("owned file is invalid or changed") from error


def _json(data: bytes) -> dict:
    try:
        value = json.loads(data, object_pairs_hook=lambda pairs: _unique(pairs))
    except (ValueError, UnicodeError) as error:
        raise MigratedWikiWorkspaceError("manifest is invalid") from error
    if not isinstance(value, dict):
        raise MigratedWikiWorkspaceError("manifest must be an object")
    return value


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


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


def _sync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _copy_file(source: Path, target: Path, limit: int) -> str:
    data = wiki_archive._read(source, limit=limit, private=True)[0]
    _write(target, data)
    return _digest(data)


def _identity_ids(manifest: dict) -> tuple[str, str, str]:
    identity = f"{manifest['installation_id']}:legacy-wiki"
    token = hashlib.sha256(identity.encode()).hexdigest()[:32]
    return f"space_wiki_{token}", f"collection_wiki_{token}", "1"


def _catalog_facts(catalog: Path, space_id: str, collection_id: str, version: str, *, include_raw: bool = False) -> dict:
    uri = "file:" + quote(str(catalog), safe="/") + "?mode=ro&immutable=1"
    with sqlite3.connect(uri, uri=True) as db:
        db.row_factory = sqlite3.Row
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise MigratedWikiWorkspaceError("owned Catalog integrity failed")
        space = db.execute("SELECT id FROM knowledge_spaces WHERE id=?", (space_id,)).fetchone()
        collection = db.execute("SELECT id,space_id,version,manifest_digest,capabilities,asset_ids FROM knowledge_datasets WHERE id=? AND space_id=? AND version=?", (collection_id, space_id, version)).fetchone()
        if space is None or collection is None:
            raise MigratedWikiWorkspaceError("Wiki Catalog identity is missing")
        try:
            asset_ids = json.loads(collection["asset_ids"])
            capabilities = json.loads(collection["capabilities"])
        except (TypeError, ValueError) as error:
            raise MigratedWikiWorkspaceError("Wiki Collection metadata is invalid") from error
        if not isinstance(asset_ids, list) or not all(isinstance(item, str) for item in asset_ids) or not isinstance(capabilities, list):
            raise MigratedWikiWorkspaceError("Wiki Collection metadata is invalid")
        assets = {}
        source_types = ("local_published_wiki", "local_wiki_raw") if include_raw else ("local_published_wiki",)
        placeholders = ",".join("?" for _ in source_types)
        for row in db.execute(f"SELECT id,space_id,kind,source_type,source_uri,revision,content_digest,metadata_json FROM knowledge_assets WHERE space_id=? AND source_type IN ({placeholders}) ORDER BY id", (space_id, *source_types)):
            try:
                metadata = json.loads(row["metadata_json"])
            except (ValueError, TypeError) as error:
                raise MigratedWikiWorkspaceError("Wiki Asset metadata is invalid") from error
            assets[row["id"]] = {"space_id": row["space_id"], "kind": row["kind"], "source_type": row["source_type"], "source_uri": row["source_uri"], "revision": row["revision"], "content_digest": row["content_digest"], "metadata": metadata}
        return {"space_id": space_id, "collection": {"id": collection["id"], "space_id": collection["space_id"], "version": collection["version"], "manifest_digest": collection["manifest_digest"], "capabilities": capabilities, "asset_ids": asset_ids}, "assets": assets}


def _bindings(manifest: dict, root: Path) -> dict[str, Path]:
    raw = manifest.get("file_bindings")
    if not isinstance(raw, dict) or not raw:
        raise MigratedWikiWorkspaceError("Wiki file bindings are invalid")
    result = {}
    evidence = _real(root / "wiki-evidence")
    for asset_id, relative in raw.items():
        if not isinstance(asset_id, str) or not isinstance(relative, str):
            raise MigratedWikiWorkspaceError("Wiki file binding is invalid")
        path = _real(root / relative)
        allowed_roots = [evidence / "archive" / "wiki"]
        if manifest["version"] in (5, 6, 7): allowed_roots.append(evidence / "archive" / "raw")
        if not any(path.is_relative_to(parent) for parent in allowed_roots) or path.is_symlink() or not path.is_file():
            raise MigratedWikiWorkspaceError("Wiki file binding escaped owned evidence")
        result[asset_id] = path
    return result


def load_migrated_wiki_workspace(root: Path | str, manifest: dict, *, _initializing_ok: bool = False, _combined: bool = False) -> dict:
    root = _real(root)
    try:
        with wiki_archive._lock(root / "wiki-evidence", shared=True):
            before = wiki_archive._verify(root / "wiki-evidence")
            result = _load_migrated_wiki_workspace(root, manifest, _initializing_ok=_initializing_ok, _combined=_combined)
            if wiki_archive._verify(root / "wiki-evidence") != before:
                raise MigratedWikiWorkspaceError("owned Wiki archive changed during load")
            return result
    except (OSError, ValueError) as error:
        raise MigratedWikiWorkspaceError("owned Wiki evidence failed verification") from error


def _load_migrated_wiki_workspace(root: Path | str, manifest: dict, *, _initializing_ok: bool = False, _combined: bool = False) -> dict:
    root = _real(root)
    if not root.is_dir() or root.stat().st_mode & 0o077 or root.stat().st_uid != os.getuid():
        raise MigratedWikiWorkspaceError("workspace root must be owned and private")
    required = {"version", "owner", "kind", "catalog", "evidence_root", "provider_id", "space_id", "collection_id", "collection_version", "archive_manifest_digest", "file_bindings", "facts", "pages", "activation_allowed"}
    if manifest.get("version") == 7: required.add("schema_evidence_digest")
    if set(manifest) != required or type(manifest.get("version")) is not int or manifest.get("version") not in (3, 5, 6, 7) or manifest.get("kind") != "migrated_wiki" or manifest.get("owner") != "puddingknowledge-local" or manifest.get("catalog") != "catalog.sqlite3" or manifest.get("evidence_root") != "wiki-evidence" or manifest.get("provider_id") != _PROVIDER or manifest.get("activation_allowed") is not False:
        raise MigratedWikiWorkspaceError("migrated Wiki workspace manifest is invalid")
    allowed = {".workspace-authority-v1.json", ".workspace-authority-v1.json.part", ".workspace.lock", "workspace.json", ".initializing", "catalog.sqlite3", "catalog.sqlite3-wal", "catalog.sqlite3-shm", "catalog.sqlite3-journal", "retrieval-traces.sqlite3", "retrieval-traces.sqlite3-wal", "retrieval-traces.sqlite3-shm", "retrieval-traces.sqlite3-journal", "processing", "wiki-evidence"}
    if manifest["version"] == 7: allowed.add("wiki-schema.json")
    if _combined: allowed.update(("blobs", "resources"))
    if any(entry.name not in allowed for entry in root.iterdir()):
        raise MigratedWikiWorkspaceError("state-dir contains unexpected entries")
    if (root / ".initializing").exists() or (root / ".initializing").is_symlink():
        if not _initializing_ok:
            raise MigratedWikiWorkspaceError("state-dir contains an incomplete initialization")
    processing = root / "processing"
    if (processing.exists() or processing.is_symlink()) and (processing.is_symlink() or not processing.is_dir() or processing.stat().st_mode & 0o077):
        raise MigratedWikiWorkspaceError("processing root is invalid")
    for name in ("catalog.sqlite3-wal", "catalog.sqlite3-shm", "catalog.sqlite3-journal", "retrieval-traces.sqlite3", "retrieval-traces.sqlite3-wal", "retrieval-traces.sqlite3-shm", "retrieval-traces.sqlite3-journal"):
        auxiliary = root / name
        if auxiliary.exists() or auxiliary.is_symlink():
            if auxiliary.is_symlink() or not auxiliary.is_file() or auxiliary.stat().st_mode & 0o077 or auxiliary.stat().st_nlink != 1:
                raise MigratedWikiWorkspaceError("owned SQLite auxiliary is invalid")
    catalog = root / "catalog.sqlite3"
    if catalog.is_symlink() or not catalog.is_file():
        raise MigratedWikiWorkspaceError("owned Wiki Catalog is unavailable")
    _read(catalog, _MAX_CATALOG)
    for suffix in ("-wal", "-shm", "-journal"):
        if (root / ("catalog.sqlite3" + suffix)).exists() or (root / ("catalog.sqlite3" + suffix)).is_symlink():
            raise MigratedWikiWorkspaceError("migrated Wiki Catalog must be checkpointed")
    evidence = root / "wiki-evidence"
    if evidence.is_symlink() or not evidence.is_dir():
        raise MigratedWikiWorkspaceError("owned Wiki evidence is unavailable")
    try:
        archive_manifest = wiki_archive.verify_archive(evidence)
    except Exception as error:
        raise MigratedWikiWorkspaceError("owned Wiki evidence failed verification") from error
    manifest_bytes = _read(evidence / "manifest.json", _MAX_MANIFEST)
    if _digest(manifest_bytes) != manifest["archive_manifest_digest"]:
        raise MigratedWikiWorkspaceError("archive manifest digest changed")
    if archive_manifest.get("activation_allowed") is not False:
        raise MigratedWikiWorkspaceError("archive activation is forbidden")
    schema_bundle = None
    if manifest["version"] == 7:
        from ..distribution.wiki_schema_evidence import verify_schema_evidence, MAX_EVIDENCE
        digest = manifest["schema_evidence_digest"]
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise MigratedWikiWorkspaceError("invalid owned schema digest")
        schema_bundle = verify_schema_evidence(_read(root / "wiki-schema.json", MAX_EVIDENCE), evidence,
                                              expected_digest=manifest["schema_evidence_digest"])
    expected_space, expected_collection, expected_version = _identity_ids(archive_manifest)
    if (manifest["space_id"], manifest["collection_id"], manifest["collection_version"]) != (expected_space, expected_collection, expected_version):
        raise MigratedWikiWorkspaceError("Wiki identity does not match archive installation")
    expected_bindings = {}
    expected_assets = {}
    for relative, fact in archive_manifest["files"].items():
        if not relative.startswith("wiki/") or not relative.endswith(".md") or relative in {"wiki/index.md", "wiki/log.md"}:
            continue
        slug = relative[5:-3]
        asset_id = "asset_wiki_" + hashlib.sha256(f"{expected_space}:{slug}".encode()).hexdigest()[:32]
        if asset_id in expected_assets:
            raise MigratedWikiWorkspaceError("Wiki Asset identity collision")
        expected_bindings[asset_id] = "wiki-evidence/archive/" + relative
        digest = "sha256:" + fact["sha256"]
        expected_assets[asset_id] = {"space_id": expected_space, "kind": "wiki_page", "source_type": "local_published_wiki",
            "source_uri": f"knowledge://spaces/{expected_space}/assets/{asset_id}", "revision": digest, "content_digest": digest,
            "metadata": {"published": True, "wiki_slug": slug, "bytes": fact["size_bytes"]}}
    page_ids = set(expected_assets)
    raw_projection = {"assets": {}, "file_bindings": {}}
    if manifest["version"] in (5, 6, 7):
        raw_projection = project_raw_assets(evidence, archive_manifest, expected_space)
        if manifest["version"] in (6, 7):
            lineage = project_wiki_lineage(evidence, archive_manifest, raw_projection["assets"])
            for asset_id, metadata in lineage.items():
                raw_projection["assets"][asset_id]["metadata"].update(metadata)
        expected_assets.update(raw_projection["assets"])
        expected_bindings.update(raw_projection["file_bindings"])
    if manifest["file_bindings"] != expected_bindings or type(manifest["pages"]) is not int or manifest["pages"] != len(page_ids):
        raise MigratedWikiWorkspaceError("Wiki bindings or page count disagree with archive")
    bindings = _bindings(manifest, root)
    facts = _catalog_facts(catalog, manifest["space_id"], manifest["collection_id"], manifest["collection_version"], include_raw=manifest["version"] in (5, 6, 7))
    if json.dumps(facts, sort_keys=True) != json.dumps(manifest["facts"], sort_keys=True):
        raise MigratedWikiWorkspaceError("owned Wiki Catalog facts changed")
    collection = facts["collection"]
    if (json.dumps(facts["assets"], sort_keys=True) != json.dumps(expected_assets, sort_keys=True)
            or len(collection["asset_ids"]) != len(page_ids) or set(collection["asset_ids"]) != page_ids
            or collection["capabilities"] != ["wiki_query"] or collection["manifest_digest"] != manifest["archive_manifest_digest"]):
        raise MigratedWikiWorkspaceError("Catalog projection disagrees with verified Wiki archive")
    if set(bindings) != set(facts["assets"]):
        raise MigratedWikiWorkspaceError("Wiki file bindings do not cover Catalog assets")
    for asset_id, path in bindings.items():
        expected = facts["assets"][asset_id]["content_digest"]
        if _digest(_read(path, wiki_archive.MAX_FILE)) != expected:
            raise MigratedWikiWorkspaceError("Wiki page digest changed")
    return {"schema_bundle": schema_bundle, "catalog": catalog, "wiki_root": evidence / "archive" / "wiki", "evidence_root": evidence, "file_bindings": bindings, "wiki_bindings": {key: bindings[key] for key in page_ids}, "raw_bindings": {key: bindings[key] for key in raw_projection["assets"]}, "space_ids": [manifest["space_id"]], "pages": manifest["pages"], "space_id": manifest["space_id"], "collection_id": manifest["collection_id"], "collection_version": manifest["collection_version"], "provider_id": _PROVIDER, "activation_allowed": False}


def bootstrap_migrated_wiki(candidate: Path | str, state_dir: Path | str, *, schema_evidence: Path | None = None) -> dict:
    candidate, root = _real(candidate), _real(state_dir)
    if candidate == root or candidate.is_relative_to(root) or root.is_relative_to(candidate):
        raise MigratedWikiWorkspaceError("candidate and owned state must be disjoint")
    schema_bytes = None
    if schema_evidence is not None:
        from ..distribution.wiki_schema_evidence import verify_schema_evidence, MAX_EVIDENCE
        schema_bytes = wiki_archive._read(schema_evidence, private=True, limit=MAX_EVIDENCE)[0]
        verify_schema_evidence(schema_bytes, candidate)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.stat().st_mode & 0o077 or root.stat().st_uid != os.getuid():
        raise MigratedWikiWorkspaceError("workspace root must be owned and private")
    if any(path.name != ".workspace.lock" for path in root.iterdir()):
        raise MigratedWikiWorkspaceError("workspace must be empty")
    marker = root / ".initializing"
    _write(marker, b"migrated Wiki bootstrap\n")
    _sync_dir(root)
    staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=root))
    try:
        with wiki_archive._lock(candidate, shared=True):
            archive_manifest = wiki_archive._verify(candidate)
            manifest_bytes = wiki_archive._read(candidate / "manifest.json", private=True, limit=_MAX_MANIFEST)[0]
            space_id, collection_id, collection_version = _identity_ids(archive_manifest)
            evidence = staging / "wiki-evidence"
            evidence.mkdir(mode=0o700)
            for name in ("plan.json", "checkpoint.json", "manifest.json", ".archive.lock"):
                _copy_file(candidate / name, evidence / name, _MAX_MANIFEST)
            inventory = wiki_archive._inventory(candidate / "archive", private=True)
            archive_root = evidence / "archive"
            archive_root.mkdir(mode=0o700)
            for directory in inventory["directories"]:
                (archive_root / directory).mkdir(mode=0o700, parents=True)
            for relative in inventory["files"]:
                target = archive_root / relative
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                _copy_file(candidate / "archive" / relative, target, wiki_archive.MAX_FILE)
            for directory in reversed(inventory["directories"]):
                _sync_dir(archive_root / directory)
            _sync_dir(archive_root)
            _sync_dir(evidence)
            if wiki_archive._verify(candidate) != archive_manifest:
                raise MigratedWikiWorkspaceError("source archive changed during copy")
            base = staging / "base.sqlite3"
            engine = create_engine(f"sqlite:///{base}")
            with engine.begin() as connection:
                migrate_to_latest(connection)
                now = datetime.now(timezone.utc).isoformat()
                connection.execute(text("INSERT INTO knowledge_spaces (id,name,description,permissions_json,created_at,updated_at) VALUES (:id,:name,'{}', '{}',:now,:now)"), {"id": space_id, "name": "Migrated Wiki", "now": now})
                connection.execute(text("INSERT INTO knowledge_datasets (id,space_id,name,version,kind,description,asset_ids,semantic_asset_ids,capabilities,freshness,permissions_json,manifest_digest,created_at,updated_at) VALUES (:id,:space,:name,:version,'wiki','', '[]','[]','[\"wiki_query\"]','{}','{}',:digest,:now,:now)"), {"id": collection_id, "space": space_id, "name": "Migrated Wiki", "version": collection_version, "digest": _digest(manifest_bytes), "now": now})
            engine.dispose()
            staged_catalog = staging / "catalog.sqlite3"
            wiki_root = archive_root / "wiki"
            raw_projection = project_raw_assets(evidence, archive_manifest, space_id)
            lineage = project_wiki_lineage(evidence, archive_manifest, raw_projection["assets"])
            for asset_id, metadata in lineage.items():
                raw_projection["assets"][asset_id]["metadata"].update(metadata)
            materialized = _materialize_catalog(base, staged_catalog, wiki_root, space_id=space_id, collection_id=collection_id, allow_empty=bool(raw_projection["assets"]))
            os.chmod(staged_catalog, 0o600)
            with sqlite3.connect(staged_catalog) as db:
                db.execute("PRAGMA foreign_keys=ON")
                for asset_id, fact in raw_projection["assets"].items():
                    db.execute("""INSERT INTO knowledge_assets
                        (id,space_id,kind,title,description,mime_type,source_type,source_uri,revision,
                         content_digest,permissions_json,metadata_json,created_at,updated_at)
                        VALUES (?,?,?,?,'','application/octet-stream',?,?,?,?, '{}',?,?,?)""",
                        (asset_id, space_id, fact["kind"], raw_projection["titles"][asset_id], fact["source_type"],
                         fact["source_uri"], fact["revision"], fact["content_digest"],
                         json.dumps(fact["metadata"], sort_keys=True), now, now))
            facts = _catalog_facts(staged_catalog, space_id, collection_id, collection_version, include_raw=True)
            bindings = {asset_id: (Path("wiki-evidence") / "archive" / "wiki" / path.relative_to(wiki_root)).as_posix() for asset_id, path in materialized["file_bindings"].items()}
            bindings.update(raw_projection["file_bindings"])
            owned = {"version": 6, "owner": "puddingknowledge-local", "kind": "migrated_wiki", "catalog": "catalog.sqlite3", "evidence_root": "wiki-evidence", "provider_id": _PROVIDER, "space_id": space_id, "collection_id": collection_id, "collection_version": collection_version, "archive_manifest_digest": _digest(manifest_bytes), "file_bindings": bindings, "facts": facts, "pages": materialized["pages"], "activation_allowed": False}
            if schema_bytes is not None:
                owned["version"] = 7
                owned["schema_evidence_digest"] = hashlib.sha256(schema_bytes).hexdigest()
                _write(staging / "wiki-schema.json", schema_bytes)
                os.replace(staging / "wiki-schema.json", root / "wiki-schema.json")
            _write(staging / "workspace.json", json.dumps(owned, sort_keys=True, separators=(",", ":")).encode())
            os.replace(staged_catalog, root / "catalog.sqlite3")
            os.replace(evidence, root / "wiki-evidence")
            os.replace(staging / "workspace.json", root / "workspace.json")
            _sync_dir(root)
            shutil.rmtree(staging, ignore_errors=True)
            loaded = load_migrated_wiki_workspace(root, owned, _initializing_ok=True)
            if wiki_archive._verify(candidate) != archive_manifest:
                raise MigratedWikiWorkspaceError("source archive changed during bootstrap")
            marker.unlink()
            _sync_dir(root)
            return loaded
    except Exception:
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
