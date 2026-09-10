"""Durable, fail-closed import of portable Knowledge Packages.

This module is deliberately independent from the legacy staging service.  A
successful import means the package has been validated, its immutable bytes
are in the local object store, and one SQLite transaction contains every
Catalog row needed to read it again after a restart.
"""
from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import shutil
import sqlite3
import stat
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

from knowledge_contracts import BlobReadRequest, BlobReadResult, CitationCandidate, Principal
from knowledge_platform.package.builder import import_package_zip
from knowledge_platform.retrieval.ports import RetrievalProviderError
from knowledge_platform.retrieval.ports import RetrievalIndexNotReady
from knowledge_platform.retrieval.local import _open_regular_file
from knowledge_platform.semantic.markdown import SemanticMarkdownDefinition, SqliteSemanticMarkdownRepository
from knowledge_platform.structured import LocalStructuredFileProvider

from .objects import LocalObjectStore


_ADMIN = {"knowledge.admin", "knowledge:admin"}
_SUPPORTED = {"knowledge_list", "knowledge_search", "knowledge_read", "knowledge_query", "wiki_query", "document_rag_query", "table_query"}
_TEXT_MIMES = {"text/plain", "text/markdown", "text/html", "application/json", "application/xml"}


def _digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _authorized(principal: Principal, spaces: set[str]) -> bool:
    scopes = set(principal.scopes)
    if principal.tenant_id is not None or not (_ADMIN & scopes):
        return False
    return all({f"knowledge.space:{space}", f"knowledge:space:{space}"} & scopes for space in spaces)


def _chunks(text: str, *, size: int = 1200) -> list[str]:
    text = text.strip()
    return [text[pos : pos + size] for pos in range(0, len(text), size)] if text else []


@dataclass(frozen=True, slots=True)
class PackageImportResult:
    package_revision: str
    package_id: str
    version: str
    spaces: tuple[str, ...]
    asset_ids: tuple[str, ...]
    collection_ids: tuple[str, ...]
    idempotent: bool = False
    capabilities: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "package_revision": self.package_revision,
            "package_id": self.package_id,
            "version": self.version,
            "spaces": list(self.spaces),
            "asset_ids": list(self.asset_ids),
            "collection_ids": list(self.collection_ids),
            "idempotent": self.idempotent,
            "capabilities": list(self.capabilities),
        }


class LocalPackagePublisher:
    """Publish package evidence into one local SQLite Catalog and object store."""

    def __init__(self, catalog: Path, state_root: Path):
        self.catalog = catalog.expanduser().absolute()
        self.state_root = state_root.expanduser().absolute()
        with _open_regular_file(self.catalog):
            pass
        self._objects = LocalObjectStore(self.state_root / "objects")
        try:
            self._ensure_schema()
        except BaseException:
            self._objects.close()
            raise

    @property
    def object_store(self) -> LocalObjectStore:
        return self._objects

    def close(self) -> None:
        self._objects.close()

    def __enter__(self) -> "LocalPackagePublisher":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _connect(self) -> sqlite3.Connection:
        self.catalog.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.catalog, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _ensure_schema(self) -> None:
        # The semantic repository owns the canonical table and its shape.
        SqliteSemanticMarkdownRepository(self.catalog)
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS knowledge_package_store_identity (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1), store_id TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS knowledge_package_imports (
                    package_revision TEXT PRIMARY KEY, package_id TEXT NOT NULL, version TEXT NOT NULL,
                    zip_digest TEXT NOT NULL, store_id TEXT NOT NULL, status TEXT NOT NULL,
                    created_at TEXT NOT NULL, completed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS knowledge_package_semantic_assets (
                    space_id TEXT NOT NULL, id TEXT NOT NULL, package_revision TEXT NOT NULL,
                    metadata_json TEXT NOT NULL, body_digest TEXT NOT NULL,
                    PRIMARY KEY(space_id,id)
                );
                CREATE TABLE IF NOT EXISTS knowledge_package_chunks (
                    asset_id TEXT NOT NULL, package_revision TEXT NOT NULL, ordinal INTEGER NOT NULL,
                    text TEXT NOT NULL, content_digest TEXT NOT NULL,
                    PRIMARY KEY(asset_id, ordinal)
                );
                """
            )
            row = db.execute("SELECT store_id FROM knowledge_package_store_identity WHERE singleton=1").fetchone()
            if row is None:
                db.execute("INSERT INTO knowledge_package_store_identity(singleton,store_id) VALUES(1,?)", (self._objects.identity,))
            elif row[0] != self._objects.identity:
                raise ValueError("Catalog is bound to a different object store")

    @staticmethod
    def _read_index(root: Path, relative: str) -> dict[str, Any]:
        value = json.loads((root / relative).read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError(f"package index is invalid: {relative}")
        return value

    def _before_commit(self, connection: sqlite3.Connection) -> None:
        """Testing seam for proving transaction rollback; production is a no-op."""

    @staticmethod
    def _table_asset(asset: Mapping[str, Any]) -> bool:
        """Return whether a package Asset is an explicitly supported table file."""
        kind = str(asset.get("kind") or "").lower()
        mime = str(asset.get("mime_type") or "").lower().split(";", 1)[0].strip()
        suffix = Path(str(asset.get("package_path") or "")).suffix.lower()
        if kind not in {"table", "spreadsheet", "structured_asset", "structured"}:
            return False
        return suffix in {".csv", ".tsv", ".xlsx", ".xls"} and mime not in {"text/plain", "text/markdown"}

    @staticmethod
    def _structured_row(profile: Any, *, asset: Mapping[str, Any], object_digest: str, revision: str, now: str, size_bytes: int) -> tuple[Any, ...]:
        aid, space = str(asset["id"]), str(asset["space_id"])
        path = str(asset["package_path"])
        return (aid, space, aid, aid, "package", Path(path).name, asset.get("sheet_name"), size_bytes, None,
                f"knowledge://spaces/{space}/structured-assets/{aid}/source", object_digest, object_digest,
                f"knowledge://spaces/{space}/structured-assets/{aid}/profile", profile.content_digest,
                profile.content_digest, "ready", profile.row_count, len(profile.columns), _json(list(profile.columns)),
                "ready", _json(["table_query"]), _json({"package_revision": revision, "package_path": path, "package_object_digest": object_digest}), now, now)

    def _snapshot_zip(self, package_zip: Path, destination: Path) -> str:
        """Read one host-bound ZIP through an anchored descriptor exactly once."""
        path = package_zip.expanduser().absolute()
        if path.is_symlink() or not path.is_file():
            raise ValueError("package ZIP must be a regular file")
        digest = hashlib.sha256()
        total = 0
        try:
            with _open_regular_file(path) as descriptor:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024 * 1024:
                    raise ValueError("package ZIP is invalid or too large")
                with destination.open("wb") as output:
                    while chunk := os.read(descriptor, 1024 * 1024):
                        total += len(chunk)
                        if total > 1024 * 1024 * 1024:
                            raise ValueError("package ZIP is too large")
                        digest.update(chunk)
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
        except RetrievalProviderError as error:
            raise ValueError("package ZIP is not a regular file") from error
        return "sha256:" + digest.hexdigest()

    async def import_package(self, principal: Principal, package_zip: Path, expected_digest: str) -> dict[str, Any]:
        if not isinstance(expected_digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", expected_digest):
            raise ValueError("expected_digest is invalid")
        if principal.tenant_id is not None or not (_ADMIN & set(principal.scopes)):
            raise PermissionError("Package import requires admin scope")
        stage_parent = self.state_root / ".package-imports"
        stage_parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        stage_dir = Path(tempfile.mkdtemp(prefix="validated-", dir=stage_parent))
        stage = stage_dir / "package"
        snapshot = stage_dir / "input.zip"
        try:
            actual_zip_digest = self._snapshot_zip(package_zip, snapshot)
            if actual_zip_digest != expected_digest:
                raise ValueError("package ZIP digest mismatch")
            validated = import_package_zip(snapshot, stage)
            root = validated.package_root
            package_manifest = self._read_index(root, "package-manifest.json")
            document = self._read_index(root, "knowledge-package.yaml")
            spaces_doc = self._read_index(root, "spaces/index.json")
            collections_doc = self._read_index(root, "collections/index.json")
            assets_doc = self._read_index(root, "assets/index.json")
            semantic_doc = self._read_index(root, "semantics/index.json")
            database_doc = self._read_index(root, "database/index.json")
            spaces = spaces_doc.get("spaces", [])
            collections = collections_doc.get("collections", [])
            assets = assets_doc.get("assets", [])
            semantics = semantic_doc.get("assets", [])
            if database_doc.get("sources"):
                raise ValueError("live database package capabilities are unsupported")
            space_ids = {str(item["id"]) for item in spaces}
            if not _authorized(principal, space_ids):
                raise PermissionError("Package import requires admin and every package Space scope")
            package_revision = str(validated.package_revision)
            package_id, version = str(document["id"]), str(document["version"])
            declared_capabilities = {str(x) for x in document.get("capabilities", [])}
            unsupported = declared_capabilities - _SUPPORTED
            if unsupported:
                raise ValueError(f"unsupported package capabilities: {sorted(unsupported)}")
            capabilities = tuple(sorted(declared_capabilities))
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                prior = connection.execute("SELECT * FROM knowledge_package_imports WHERE package_revision=?", (package_revision,)).fetchone()
                if prior is not None:
                    if prior["status"] != "complete":
                        raise ValueError("package revision has incomplete publication")
                    self._assert_complete(connection, assets, collections, semantics, package_revision)
                    connection.commit()
                    return PackageImportResult(package_revision, package_id, version, tuple(sorted(space_ids)), tuple(sorted(str(a["id"]) for a in assets)), tuple(sorted(str(c["id"]) for c in collections)), True, capabilities).as_dict()
                self._check_conflicts(connection, assets, collections, semantics, package_revision)
                identity = connection.execute("SELECT package_revision FROM knowledge_package_imports WHERE package_id=? AND version=?", (package_id, version)).fetchone()
                if identity is not None and identity[0] != package_revision:
                    raise ValueError('Package id/version already has different content')
                # Write all immutable bytes before the SQL transaction commits.  Orphans are harmless;
                # no Catalog row can point at them after rollback.
                asset_objects: dict[str, str] = {}
                for asset in assets:
                    path = root / str(asset["package_path"])
                    content = path.read_bytes()
                    if "sha256:" + hashlib.sha256(content).hexdigest() != asset["content_digest"]:
                        raise ValueError(f"asset object digest mismatch: {asset['id']}")
                    asset_objects[str(asset["id"])] = self._objects.put(content)
                semantic_objects: dict[str, str] = {}
                for semantic in semantics:
                    content = (root / str(semantic["package_path"])).read_bytes()
                    semantic_objects[str(semantic["id"])] = self._objects.put(content)
                now = datetime.now(UTC).isoformat()
                connection.execute("INSERT INTO knowledge_package_imports VALUES(?,?,?,?,?,?,?,?)", (package_revision, package_id, version, actual_zip_digest, self._objects.identity, "publishing", now, now))
                for asset in assets:
                    aid, sid = str(asset["id"]), str(asset["space_id"])
                    content = self._objects.read(asset_objects[aid])
                    mime = str(asset.get("mime_type") or "")
                    chunk_count = len(_chunks(content.decode("utf-8"))) if (asset["kind"] in {"document", "wiki_page"} and (mime in _TEXT_MIMES or mime.startswith("text/"))) else 0
                    metadata = {"package_revision": package_revision, "package_id": package_id, "source_type": "package", "object_digest": asset_objects[aid], "package_path": asset["package_path"], "chunk_count": chunk_count}
                    if asset.get("sheet_name") is not None:
                        metadata["sheet_name"] = asset["sheet_name"]
                    for field in ("original_asset_id", "derivatives", "published_asset_ids"):
                        if field in asset:
                            metadata[field] = asset[field]
                    connection.execute(
                        "INSERT INTO knowledge_assets(id,space_id,kind,title,description,mime_type,source_type,source_uri,revision,content_digest,permissions_json,metadata_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (aid, sid, asset["kind"], asset["title"], asset.get("description", ""), asset["mime_type"], "package", asset["source_uri"], asset["revision"], asset["content_digest"], "{}", _json(metadata), now, now),
                    )
                    if self._table_asset(asset):
                        profile = LocalStructuredFileProvider(asset_paths={}, asset_uris={}).inspect_source(
                            path=root / str(asset["package_path"]), sheet_name=asset.get("sheet_name")
                        )
                        if profile.content_digest != asset["content_digest"]:
                            raise ValueError(f"structured profile digest mismatch: {aid}")
                        connection.execute(
                            "INSERT INTO knowledge_structured_assets (id,space_id,source_key,document_asset_id,source_type,file_name,sheet_name,size_bytes,modified_at,source_uri,source_reference_digest,logical_path_digest,profile_uri,profile_reference_digest,content_digest,profile_status,row_count,column_count,columns_json,reference_status,capabilities,metadata_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            self._structured_row(profile, asset=asset, object_digest=asset_objects[aid], revision=package_revision, now=now, size_bytes=len(content)),
                        )
                    mime = str(asset.get("mime_type") or "")
                    if asset["kind"] in {"document", "wiki_page"} and (mime in _TEXT_MIMES or mime.startswith("text/")):
                        text = self._objects.read(asset_objects[aid]).decode("utf-8")
                        for ordinal, chunk in enumerate(_chunks(text)):
                            connection.execute("INSERT INTO knowledge_package_chunks VALUES(?,?,?,?,?)", (aid, package_revision, ordinal, chunk, "sha256:" + hashlib.sha256(chunk.encode()).hexdigest()))
                for collection in collections:
                    cid, sid, cv = str(collection["id"]), str(collection["space_id"]), str(collection["version"])
                    collection_capabilities = {str(x) for x in collection.get("capabilities", [])}
                    if collection_capabilities - _SUPPORTED:
                        raise ValueError(f"unsupported Collection capabilities: {sorted(collection_capabilities - _SUPPORTED)}")
                    supported = sorted(collection_capabilities)
                    connection.execute("INSERT INTO knowledge_datasets(id,space_id,name,version,kind,description,asset_ids,semantic_asset_ids,capabilities,freshness,permissions_json,manifest_digest,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (cid, sid, collection["name"], cv, collection["kind"], "", _json(collection.get("asset_ids", [])), _json(collection.get("semantic_asset_ids", [])), _json(supported), _json(collection.get("freshness", {})), "{}", package_revision, now, now))
                    for capability in supported:
                        if capability in {"wiki_query", "document_rag_query"}:
                            binding = {"provider_id": "knowledge_package"}
                        elif capability == "table_query":
                            candidates = [a for a in assets if str(a.get("id")) in {str(x) for x in collection.get("asset_ids", [])} and self._table_asset(a)]
                            if len(candidates) != 1:
                                raise ValueError("table_query Collection must select exactly one table Asset")
                            binding = {"asset_id": str(candidates[0]["id"])}
                        else:
                            continue
                        connection.execute("INSERT INTO knowledge_collection_bindings VALUES(?,?,?,?,?,?,?)", (sid, cid, cv, capability, _json(binding), now, now))
                for semantic in semantics:
                    body = self._objects.read(semantic_objects[str(semantic["id"])])
                    prefix, _, markdown = body.decode("utf-8").partition("\n---\n\n")
                    frontmatter = json.loads(prefix.removeprefix("---\n"))
                    definition = SemanticMarkdownDefinition(id=str(semantic["id"]), space_id=str(semantic["space_id"]), semantic_type=str(semantic["type"]), name=str(semantic["name"]), description=str(semantic.get("description", "")), aliases=tuple(semantic.get("aliases", [])), tags=tuple(semantic.get("tags", [])), frontmatter=semantic.get("frontmatter", {}), body=markdown)
                    definition_digest = definition.definition_digest
                    actor_digest = "sha256:" + hashlib.sha256(("package-import:" + principal.subject_id).encode()).hexdigest()
                    decision_digest = "sha256:" + hashlib.sha256(("package-decision:" + package_revision + ":" + str(semantic["id"])).encode()).hexdigest()
                    existing_semantic = connection.execute("SELECT definition_digest,body,status FROM knowledge_semantic_assets WHERE space_id=? AND id=?", (semantic["space_id"], semantic["id"])).fetchone()
                    if existing_semantic is not None:
                        if existing_semantic[0] != definition_digest or existing_semantic[1] != markdown or existing_semantic[2] != "active":
                            raise ValueError(f"Semantic identity conflict: {semantic['id']}")
                    else:
                        connection.execute("INSERT INTO knowledge_semantic_assets(space_id,id,type,name,description,aliases,tags,frontmatter,body,status,definition_digest,created_at,updated_at,actor_digest,decision_digest) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (semantic["space_id"], semantic["id"], semantic["type"], semantic["name"], semantic.get("description", ""), _json(semantic.get("aliases", [])), _json(semantic.get("tags", [])), _json(semantic.get("frontmatter", {})), markdown, "active", definition_digest, now, now, actor_digest, decision_digest))
                    connection.execute("INSERT INTO knowledge_package_semantic_assets VALUES(?,?,?,?,?)", (semantic["space_id"], semantic["id"], package_revision, _json(semantic), semantic_objects[str(semantic["id"])]))
                self._before_commit(connection)
                connection.execute("UPDATE knowledge_package_imports SET status='complete', completed_at=? WHERE package_revision=?", (datetime.now(UTC).isoformat(), package_revision))
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()
            return PackageImportResult(package_revision, package_id, version, tuple(sorted(space_ids)), tuple(sorted(str(a["id"]) for a in assets)), tuple(sorted(str(c["id"]) for c in collections)), False, capabilities).as_dict()
        finally:
            shutil.rmtree(stage_dir, ignore_errors=True)

    def _check_conflicts(self, connection: sqlite3.Connection, assets: list[Mapping[str, Any]], collections: list[Mapping[str, Any]], semantics: list[Mapping[str, Any]], revision: str) -> None:
        for asset in assets:
            row = connection.execute("SELECT space_id,source_type,content_digest,metadata_json FROM knowledge_assets WHERE id=?", (asset["id"],)).fetchone()
            if row is not None and (row["source_type"] != "package" or row["content_digest"] != asset["content_digest"] or json.loads(row["metadata_json"]).get("package_revision") != revision):
                raise ValueError(f"Asset identity conflict: {asset['id']}")
            if self._table_asset(asset):
                structured = connection.execute("SELECT source_type,document_asset_id,content_digest,columns_json,row_count,sheet_name,metadata_json FROM knowledge_structured_assets WHERE id=?", (asset["id"],)).fetchone()
                if structured is not None and (structured["source_type"] != "package" or structured["document_asset_id"] != asset["id"]):
                    raise ValueError(f"Structured Asset identity conflict: {asset['id']}")
        for collection in collections:
            row = connection.execute("SELECT manifest_digest FROM knowledge_datasets WHERE id=? AND space_id=? AND version=?", (collection["id"], collection["space_id"], collection["version"])).fetchone()
            if row is not None and row[0] != revision:
                raise ValueError(f"Collection identity conflict: {collection['id']}/{collection['version']}")
        for semantic in semantics:
            row = connection.execute("SELECT package_revision,metadata_json FROM knowledge_package_semantic_assets WHERE id=? AND space_id=?", (semantic["id"], semantic["space_id"])).fetchone()
            if row is not None and row[0] != revision:
                raise ValueError(f"Semantic identity conflict: {semantic['id']}")

    def _assert_complete(self, connection: sqlite3.Connection, assets: list[Mapping[str, Any]], collections: list[Mapping[str, Any]], semantics: list[Mapping[str, Any]], revision: str) -> None:
        for asset in assets:
            row = connection.execute("SELECT * FROM knowledge_assets WHERE id=?", (asset["id"],)).fetchone()
            if row is None or json.loads(row['metadata_json']).get("package_revision") != revision:
                raise ValueError("package revision is incomplete")
            if row['source_type'] != 'package' or any(row[field] != asset[field] for field in
                    ('space_id', 'kind', 'title', 'mime_type', 'source_uri', 'revision', 'content_digest')):
                raise ValueError('package Asset publication changed')
            metadata = json.loads(row['metadata_json'])
            if any(metadata.get(field) != asset.get(field) for field in ('original_asset_id', 'derivatives', 'published_asset_ids')):
                raise ValueError('package Asset relations changed')
            data = self._objects.read(metadata["object_digest"])
            if "sha256:" + hashlib.sha256(data).hexdigest() != asset["content_digest"]:
                raise ValueError("package revision object is corrupt")
            if self._table_asset(asset):
                structured = connection.execute("SELECT * FROM knowledge_structured_assets WHERE id=?", (asset["id"],)).fetchone()
                if structured is None or structured["source_type"] != "package" or structured["document_asset_id"] != asset["id"]:
                    raise ValueError("package structured Asset is incomplete")
                suffix = Path(str(asset.get("package_path") or "")).suffix
                replay_dir = Path(tempfile.mkdtemp(prefix=".replay-", dir=self.state_root))
                try:
                    replay_path = replay_dir / ("source" + suffix)
                    replay_path.write_bytes(data)
                    profile = LocalStructuredFileProvider(asset_paths={}, asset_uris={}).inspect_source(path=replay_path, sheet_name=asset.get("sheet_name"))
                finally:
                    shutil.rmtree(replay_dir, ignore_errors=True)
                expected_uri = f"knowledge://spaces/{asset['space_id']}/structured-assets/{asset['id']}/source"
                metadata = json.loads(structured["metadata_json"])
                capabilities = json.loads(structured["capabilities"])
                if (structured["content_digest"] != profile.content_digest
                        or structured["row_count"] != profile.row_count
                        or json.loads(structured["columns_json"]) != list(profile.columns)
                        or structured["space_id"] != asset["space_id"]
                        or structured["source_uri"] != expected_uri
                        or structured["sheet_name"] != asset.get("sheet_name")
                        or structured["profile_status"] != "ready"
                        or structured["reference_status"] != "ready"
                        or capabilities != ["table_query"]
                        or metadata.get("package_revision") != revision
                        or metadata.get("package_object_digest") != "sha256:" + hashlib.sha256(data).hexdigest()
                        or structured["source_reference_digest"] != metadata.get("package_object_digest")
                        or structured["logical_path_digest"] != metadata.get("package_object_digest")
                        or structured["profile_reference_digest"] != profile.content_digest):
                    raise ValueError("package structured profile publication changed")
            if asset["kind"] in {"document", "wiki_page"} and (str(asset.get("mime_type", "")).startswith("text/") or asset.get("mime_type") in _TEXT_MIMES):
                expected = _chunks(data.decode("utf-8"))
                rows = connection.execute("SELECT ordinal,text,content_digest FROM knowledge_package_chunks WHERE asset_id=? AND package_revision=? ORDER BY ordinal", (asset["id"], revision)).fetchall()
                if len(rows) != len(expected) or any(r[0] != i or r[1] != expected[i] or r[2] != "sha256:" + hashlib.sha256(expected[i].encode()).hexdigest() for i, r in enumerate(rows)):
                    raise ValueError("package chunks are incomplete")
        for collection in collections:
            current = connection.execute("SELECT * FROM knowledge_datasets WHERE id=? AND space_id=? AND version=? AND manifest_digest=?", (collection["id"], collection["space_id"], collection["version"], revision)).fetchone()
            if current is None:
                raise ValueError("package revision is incomplete")
            if (current['name'] != collection['name'] or current['kind'] != collection['kind']
                    or any(json.loads(current[field]) != collection.get(field, [])
                           for field in ('asset_ids', 'semantic_asset_ids', 'capabilities'))):
                raise ValueError('package Collection publication changed')
            for capability in set(collection.get("capabilities", [])) & {"wiki_query", "document_rag_query"}:
                binding = connection.execute("SELECT binding_json FROM knowledge_collection_bindings WHERE space_id=? AND collection_id=? AND collection_version=? AND capability=?", (collection["space_id"], collection["id"], collection["version"], capability)).fetchone()
                if binding is None or json.loads(binding[0]) != {"provider_id": "knowledge_package"}:
                    raise ValueError("package provider binding is incomplete")
            if "table_query" in set(collection.get("capabilities", [])):
                candidates = [a for a in assets if str(a.get("id")) in {str(x) for x in collection.get("asset_ids", [])} and self._table_asset(a)]
                if len(candidates) != 1:
                    raise ValueError("table_query Collection must select exactly one table Asset")
                binding = connection.execute("SELECT binding_json FROM knowledge_collection_bindings WHERE space_id=? AND collection_id=? AND collection_version=? AND capability='table_query'", (collection["space_id"], collection["id"], collection["version"])).fetchone()
                if binding is None or json.loads(binding[0]) != {"asset_id": str(candidates[0]["id"])}:
                    raise ValueError("package table provider binding is incomplete")
        for semantic in semantics:
            owned = connection.execute("SELECT body_digest FROM knowledge_package_semantic_assets WHERE id=? AND space_id=? AND package_revision=?", (semantic["id"], semantic["space_id"], revision)).fetchone()
            row = connection.execute("SELECT * FROM knowledge_semantic_assets WHERE id=? AND space_id=?", (semantic["id"], semantic["space_id"])).fetchone()
            if owned is None or row is None:
                raise ValueError("package revision is incomplete")
            payload = self._objects.read(owned[0])
            if 'sha256:' + hashlib.sha256(payload).hexdigest() != semantic['content_digest']:
                raise ValueError('package semantic object is corrupt')
            _, separator, markdown = payload.decode('utf-8').partition('\n---\n\n')
            if not separator:
                raise ValueError('package semantic body is invalid')
            expected = SemanticMarkdownDefinition(id=semantic['id'], space_id=semantic['space_id'],
                semantic_type=semantic['type'], name=semantic['name'], description=semantic.get('description', ''),
                aliases=tuple(semantic.get('aliases', [])), tags=tuple(semantic.get('tags', [])),
                frontmatter=semantic.get('frontmatter', {}), body=markdown)
            current = SqliteSemanticMarkdownRepository._decode(row)
            if (current.status != 'active' or current.definition.definition_digest != expected.definition_digest
                    or current.definition_digest != expected.definition_digest):
                raise ValueError('package semantic publication changed')

    def derivative_targets(self, asset_id: str) -> dict:
        with self._connect() as db:
            row = db.execute("SELECT source_type,metadata_json FROM knowledge_assets WHERE id=?", (asset_id,)).fetchone()
        if row is None or row["source_type"] != "package":
            return {}
        self.read_published(asset_id)
        targets = json.loads(row["metadata_json"]).get("derivatives", {})
        for target in targets.values():
            self.read_published(target)
        return targets

    def read_published(self, asset_id: str) -> bytes:
        return self._read_published_range(asset_id, 0, None)[0]

    def _read_published_range(self, asset_id: str, start: int, end: int | None) -> tuple[bytes, str]:
        with self._connect() as connection:
            row = connection.execute("SELECT source_type,content_digest,metadata_json FROM knowledge_assets WHERE id=?", (asset_id,)).fetchone()
        if row is None or row["source_type"] != "package":
            raise LookupError("published package Asset does not exist")
        metadata = json.loads(row["metadata_json"])
        with self._connect() as connection:
            complete = connection.execute("SELECT status FROM knowledge_package_imports WHERE package_revision=?", (metadata.get("package_revision"),)).fetchone()
        if complete is None or complete[0] != "complete":
            raise ValueError("published package is not complete")
        data = self._objects.read(metadata["object_digest"])
        if "sha256:" + hashlib.sha256(data).hexdigest() != row["content_digest"]:
            raise ValueError("published package object integrity mismatch")
        return data[start:end], row["content_digest"]


class PackageRetrievalProvider:
    """Small retrieval provider backed only by package-owned chunks."""

    def __init__(self, publisher: LocalPackagePublisher, repository: Any | None = None, *, asset_ids: tuple[str, ...] | None = None):
        self.publisher, self.repository, self.asset_ids = publisher, repository, asset_ids

    async def search(self, *, query: str, space_id: str | None, limit: int) -> tuple[CitationCandidate, ...]:
        if not query or limit <= 0:
            return ()
        with self.publisher._connect() as db:
            params: list[Any] = [query]
            where = "i.status='complete' AND a.source_type='package' AND json_extract(a.metadata_json, '$.package_revision')=c.package_revision AND instr(c.text, ?) > 0"
            if space_id is not None:
                where += " AND a.space_id = ?"
                params.append(space_id)
            if self.asset_ids is not None:
                if not self.asset_ids:
                    return ()
                marks = ",".join("?" for _ in self.asset_ids)
                where += f" AND c.asset_id IN ({marks})"
                params.extend(self.asset_ids)
            params.append(int(limit))
            check_params: list[Any] = []
            check_where = "i.status='complete' AND a.source_type='package'"
            if space_id is not None:
                check_where += " AND a.space_id=?"
                check_params.append(space_id)
            if self.asset_ids is not None:
                if not self.asset_ids:
                    return ()
                check_where += " AND a.id IN (" + ",".join("?" for _ in self.asset_ids) + ")"
                check_params.extend(self.asset_ids)
            expected_rows = db.execute(f"SELECT a.id,a.metadata_json FROM knowledge_assets a JOIN knowledge_package_imports i ON i.package_revision=json_extract(a.metadata_json,'$.package_revision') WHERE {check_where}", check_params).fetchall()
            for expected in expected_rows:
                expected_count = int(json.loads(expected["metadata_json"]).get("chunk_count", 0))
                actual_count = db.execute("SELECT COUNT(*) FROM knowledge_package_chunks WHERE asset_id=?", (expected["id"],)).fetchone()[0]
                if actual_count != expected_count:
                    raise RetrievalIndexNotReady("package chunk index is incomplete")
            rows = db.execute(f"SELECT c.asset_id,c.ordinal,c.text,c.content_digest,a.source_uri,a.content_digest AS asset_digest FROM knowledge_package_chunks c JOIN knowledge_assets a ON a.id=c.asset_id JOIN knowledge_package_imports i ON i.package_revision=c.package_revision WHERE {where} ORDER BY c.asset_id,c.ordinal LIMIT ?", params).fetchall()
            result = []
            for row in rows:
                try:
                    if "sha256:" + hashlib.sha256(row["text"].encode()).hexdigest() != row["content_digest"]:
                        raise RetrievalProviderError("package chunk integrity mismatch")
                    original, original_digest = self.publisher._read_published_range(row["asset_id"], 0, None)
                    if original_digest != row["asset_digest"] or "sha256:" + hashlib.sha256(original).hexdigest() != row["asset_digest"]:
                        raise RetrievalProviderError("package original integrity mismatch")
                    if row["text"] not in original.decode("utf-8"):
                        raise RetrievalProviderError("package chunk is not an original substring")
                except (UnicodeDecodeError, LookupError, ValueError) as error:
                    if isinstance(error, RetrievalProviderError):
                        raise
                    raise RetrievalProviderError("package evidence integrity validation failed") from error
                if query not in row["text"]:
                    continue
                result.append(CitationCandidate(row["asset_id"], row["source_uri"], quote=row["text"][:1200], locator={"chunk_id": f"{row['asset_id']}:chunk_{row['ordinal']}"}, score=1.0))
            return tuple(result)


class PackageBlobReader:
    def __init__(self, repository: Any, publisher: LocalPackagePublisher, fallback: Any | None = None):
        self.repository, self.publisher, self.fallback = repository, publisher, fallback

    async def read(self, request: BlobReadRequest) -> BlobReadResult:
        parts = request.resource_uri.removeprefix("knowledge://").split("/")
        if len(parts) != 4 or parts[0] != "spaces" or parts[2] != "assets":
            raise RetrievalProviderError("package Asset URI is invalid")
        space_id, asset_id = parts[1], parts[3]
        if request.principal.tenant_id is not None or not ({f"knowledge.space:{space_id}", f"knowledge:space:{space_id}"} & set(request.principal.scopes)):
            raise RetrievalProviderError("package Asset read requires Space scope")
        try:
            with self.publisher._connect() as db:
                row = db.execute("SELECT space_id,source_type,source_uri FROM knowledge_assets WHERE id=?", (asset_id,)).fetchone()
            if row is None or row["source_type"] != "package":
                if self.fallback is not None:
                    return await self.fallback.read(request)
                raise RetrievalProviderError("package Asset binding is unavailable")
            if row["space_id"] != space_id or row["source_uri"] != request.resource_uri:
                raise RetrievalProviderError("package Asset binding is invalid")
            content, asset_digest = self.publisher._read_published_range(asset_id, request.start, request.end)
        except LookupError:
            if self.fallback is None:
                raise
            return await self.fallback.read(request)
        if request.expected_digest is not None and request.expected_digest != asset_digest:
            raise RetrievalProviderError("published package Asset digest mismatch")
        return BlobReadResult(resource_uri=request.resource_uri, content=content, content_digest="sha256:" + hashlib.sha256(content).hexdigest(), start=request.start, end=request.start + len(content), asset_digest=asset_digest)

__all__ = ["LocalPackagePublisher", "PackageImportResult", "PackageRetrievalProvider", "PackageBlobReader"]
