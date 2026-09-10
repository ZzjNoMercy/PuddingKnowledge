"""Read-only SQLite implementation of the Catalog query repository port."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .query import CatalogQueryRepository


@dataclass(frozen=True, slots=True)
class _SqlitePackageSnapshot:
    catalog_revision: str
    spaces: list[dict[str, Any]]
    collections: list[dict[str, Any]]
    assets: list[dict[str, Any]]
    semantic_assets: list[dict[str, Any]]
    database_sources: list[dict[str, Any]]
    provider_versions: dict[str, str]


class SqliteCatalogQueryRepository(CatalogQueryRepository):
    """Query only the independent Platform Catalog database."""

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path.expanduser().absolute()
        if self._database_path.is_symlink() or not self._database_path.is_file():
            raise FileNotFoundError(f"Catalog database does not exist: {self._database_path}")

    @property
    def catalog_revision(self) -> str:
        digest = hashlib.sha256()
        for candidate in (
            self._database_path,
            Path(f"{self._database_path}-wal"),
            Path(f"{self._database_path}-shm"),
        ):
            if candidate.is_symlink():
                raise OSError("Catalog sidecar must not be a symlink")
            if not candidate.is_file():
                continue
            digest.update(candidate.name.encode("utf-8"))
            digest.update(b"\0")
            with candidate.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
        return f"sha256:{digest.hexdigest()}"

    def read_package_snapshot(self) -> _SqlitePackageSnapshot:
        """Read all Package inputs in one SQLite snapshot and bind its revision."""

        revision_before = self.catalog_revision
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            spaces = [
                {"id": str(row["id"]), "name": str(row["name"] or ""), "description": str(row["description"] or "")}
                for row in connection.execute("SELECT id, name, description FROM knowledge_spaces ORDER BY id")
            ]
            semantic_asset_column = (
                "semantic_asset_ids"
                if self._has_column(connection, "knowledge_datasets", "semantic_asset_ids")
                else "NULL AS semantic_asset_ids"
            )
            collection_select = "SELECT id, space_id, name, version, kind, capabilities, freshness, asset_ids, " + semantic_asset_column
            bindings = self._collection_bindings(connection)
            collections = [
                self._collection_with_bindings(row, bindings)
                for row in connection.execute(collection_select + " FROM knowledge_datasets ORDER BY space_id, id, version")
            ]
            self._attach_database_source_relations(connection, collections)
            assets = [
                self._asset(row)
                for row in connection.execute(
                    "SELECT id, space_id, kind, title, description, mime_type, source_type, source_uri, revision, content_digest "
                    "FROM knowledge_assets ORDER BY id"
                )
            ]
            assets = self._merge_structured_package_assets(connection, assets)
            database_sources = self._database_package_sources(connection, collections)
            revision_after = self.catalog_revision
            if revision_before != revision_after:
                raise OSError("Catalog changed while Package snapshot was being read")
            semantic_assets = self._semantic_assets_from_connection(connection)
            # Connector rows are intentionally not Package/Vanna inputs: they
            # contain connection metadata, while Vanna rebuild requires a
            # separately materialized, portable evidence index.  Until that
            # index is present in the Catalog schema, expose an empty source
            # list rather than leaking or guessing from connector fields.
            return _SqlitePackageSnapshot(revision_before, spaces, collections, assets, semantic_assets, database_sources, {})
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        uri = f"file:{quote(str(self._database_path), safe='/')}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA query_only=ON")
        return connection

    @classmethod
    def _attach_database_source_relations(cls, connection: sqlite3.Connection, collections: list[dict[str, Any]]) -> None:
        if not cls._has_table(connection, "knowledge_package_database_collections"):
            return
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(knowledge_package_database_collections)")}
        required = {"space_id", "collection_id", "collection_version", "source_id"}
        if not required.issubset(columns):
            raise ValueError("knowledge_package_database_collections schema is incomplete")
        by_key = {(str(item["space_id"]), str(item["id"]), str(item["version"])): item for item in collections}
        for row in connection.execute("SELECT space_id, collection_id, collection_version, source_id FROM knowledge_package_database_collections ORDER BY source_id"):
            key = (str(row["space_id"]), str(row["collection_id"]), str(row["collection_version"]))
            collection = by_key.get(key)
            if collection is None:
                raise ValueError("database source relation references an unknown Collection")
            collection.setdefault("database_source_ids", []).append(str(row["source_id"]))
        for collection in collections:
            ids = collection.get("database_source_ids", [])
            if len(ids) != len(set(ids)):
                raise ValueError("database source relation is duplicated")

    @classmethod
    def _database_package_sources(cls, connection: sqlite3.Connection, collections: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not cls._has_table(connection, "knowledge_package_database_sources"):
            return []
        if not cls._has_table(connection, "knowledge_package_imports"):
            raise ValueError("database Package source import ledger is unavailable")
        required = {"id", "space_id", "dataset_id", "package_revision", "source_json", "content_digest"}
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(knowledge_package_database_sources)")}
        if not required.issubset(columns):
            raise ValueError("knowledge_package_database_sources schema is incomplete")
        selected: set[tuple[str, str]] = set()
        selected_source_ids: set[str] = set()
        for collection in collections:
            explicit_source_ids = {str(value) for value in collection.get("database_source_ids", [])}
            selected_source_ids.update(explicit_source_ids)
            if explicit_source_ids:
                continue
            binding = collection.get("provider_bindings", {}).get("database_nl2sql") if isinstance(collection.get("provider_bindings"), dict) else None
            dataset_id = binding.get("dataset_id") if isinstance(binding, dict) else collection.get("dataset_id")
            if not dataset_id:
                continue
            selected.add((str(collection.get("space_id") or ""), str(dataset_id)))
        if not selected and not selected_source_ids:
            return []
        available_rows = connection.execute("SELECT id, space_id, dataset_id, package_revision FROM knowledge_package_database_sources").fetchall()
        complete_revisions = {str(row[0]) for row in connection.execute("SELECT package_revision FROM knowledge_package_imports WHERE status='complete'")}
        for row in available_rows:
            key = (str(row["space_id"] or ""), str(row["dataset_id"] or ""))
            if (str(row["id"]) in selected_source_ids or key in selected) and str(row["package_revision"]) not in complete_revisions:
                raise ValueError(f"database Package source import is incomplete: {row['id']}")
        result: list[dict[str, Any]] = []
        for row in connection.execute("SELECT s.id, s.space_id, s.dataset_id, s.package_revision, s.source_json, s.content_digest FROM knowledge_package_database_sources s JOIN knowledge_package_imports i ON i.package_revision=s.package_revision AND i.status='complete' ORDER BY s.id"):
            key = (str(row["space_id"] or ""), str(row["dataset_id"] or ""))
            if str(row["id"]) not in selected_source_ids and key not in selected:
                continue
            try:
                source = json.loads(row["source_json"])
            except (TypeError, json.JSONDecodeError) as error:
                raise ValueError(f"database Package source JSON is invalid: {row['id']}") from error
            if not isinstance(source, dict) or set(source) - {"id", "space_id", "dataset_id", "dialect", "ddl", "documentation", "sql_examples", "entities"}:
                raise ValueError(f"database Package source is not portable: {row['id']}")
            if (str(source.get("id")), str(source.get("space_id")), str(source.get("dataset_id"))) != (str(row["id"]), key[0], key[1]):
                raise ValueError(f"database Package source identity mismatch: {row['id']}")
            encoded = json.dumps(source, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            if str(row["content_digest"]) != "sha256:" + hashlib.sha256(encoded).hexdigest():
                raise ValueError(f"database Package source digest mismatch: {row['id']}")
            revision = str(row["package_revision"] or "")
            if len(revision) != 71 or not revision.startswith("sha256:") or any(char not in "0123456789abcdef" for char in revision[7:]):
                raise ValueError(f"database Package source revision is invalid: {row['id']}")
            result.append(source)
        return result

    @classmethod
    def _merge_structured_package_assets(
        cls, connection: sqlite3.Connection, assets: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Project publishable table sources into the Package asset index."""
        if not cls._has_table(connection, "knowledge_structured_assets"):
            return assets
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(knowledge_structured_assets)")}
        required = {"id", "space_id", "file_name", "sheet_name", "source_uri", "content_digest", "reference_status", "capabilities"}
        if not required.issubset(columns):
            raise ValueError("knowledge_structured_assets schema is incomplete for Package export")
        rows = connection.execute(
            "SELECT id, space_id, file_name, sheet_name, source_uri, content_digest, reference_status, capabilities "
            "FROM knowledge_structured_assets ORDER BY id"
        ).fetchall()
        by_id = {str(item["id"]): item for item in assets}
        suffixes = {
            ".md": "text/markdown", ".markdown": "text/markdown", ".csv": "text/csv", ".tsv": "text/tab-separated-values",
            ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xls": "application/vnd.ms-excel",
            ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        }
        for row in rows:
            status = str(row["reference_status"] or "").casefold()
            capabilities = cls._json(row["capabilities"], field="knowledge_structured_assets.capabilities", default=[])
            if status not in {"ready", "verified", "active"} or not isinstance(capabilities, list) or "table_query" not in {str(value) for value in capabilities}:
                continue
            asset_id = str(row["id"] or "")
            space_id = str(row["space_id"] or "")
            digest = str(row["content_digest"] or "")
            if not asset_id or not space_id or not digest:
                raise ValueError("publishable structured asset metadata is incomplete")
            existing = by_id.get(asset_id)
            if existing is not None:
                if str(existing.get("space_id")) != space_id or str(existing.get("content_digest")) != digest:
                    raise ValueError(f"structured asset conflicts with Catalog Asset: {asset_id}")
                # The structured row is the canonical owner of table binding
                # metadata.  A document Asset with the same id/digest can
                # still have a different sheet, so preserve this field rather
                # than silently dropping it on projection.
                if row["sheet_name"]:
                    existing["sheet_name"] = str(row["sheet_name"])
                continue
            file_name = str(row["file_name"] or "")
            extension = Path(file_name).suffix.casefold()
            mime_type = suffixes.get(extension, "application/octet-stream")
            canonical_uri = f"knowledge://spaces/{space_id}/assets/{asset_id}"
            projected = {
                "id": asset_id,
                "space_id": space_id,
                "kind": "structured_asset",
                "title": file_name,
                "description": "",
                "mime_type": mime_type,
                "source_type": "structured_file",
                "source_uri": canonical_uri,
                "revision": digest,
                "content_digest": digest,
            }
            if row["sheet_name"]:
                projected["sheet_name"] = str(row["sheet_name"])
            assets.append(projected)
            by_id[asset_id] = projected
        return assets

    @staticmethod
    def _has_column(connection: sqlite3.Connection, table: str, column: str) -> bool:
        return any(str(row[1]) == column for row in connection.execute(f'PRAGMA table_info("{table}")'))

    @staticmethod
    def _has_table(connection: sqlite3.Connection, table: str) -> bool:
        return connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone() is not None

    @classmethod
    def _semantic_assets_from_connection(cls, connection: sqlite3.Connection) -> list[dict[str, Any]]:
        if not cls._has_table(connection, "knowledge_semantic_assets"):
            return []
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(knowledge_semantic_assets)")}
        status_filter = " WHERE status = 'active'" if "status" in columns else ""
        rows = connection.execute(
            "SELECT id, space_id, type, name, description, aliases, tags, frontmatter, body "
            f"FROM knowledge_semantic_assets{status_filter} ORDER BY id"
        ).fetchall()
        result = []
        for row in rows:
            aliases = cls._json(row["aliases"], field="knowledge_semantic_assets.aliases", default=[])
            tags = cls._json(row["tags"], field="knowledge_semantic_assets.tags", default=[])
            frontmatter = cls._json(row["frontmatter"], field="knowledge_semantic_assets.frontmatter", default={})
            if not isinstance(aliases, list) or not isinstance(tags, list) or not isinstance(frontmatter, dict):
                raise ValueError("Semantic Asset JSON fields have an invalid shape")
            result.append(
                {
                    "id": str(row["id"]),
                    "space_id": str(row["space_id"]),
                    "type": str(row["type"]),
                    "name": str(row["name"] or ""),
                    "description": str(row["description"] or ""),
                    "aliases": [str(item) for item in aliases],
                    "tags": [str(item) for item in tags],
                    "frontmatter": frontmatter,
                    "body": str(row["body"] or ""),
                }
            )
        return result

    @staticmethod
    def _json(value: Any, *, field: str, default: Any) -> Any:
        if value in (None, ""):
            return default
        try:
            return json.loads(value)
        except (TypeError, json.JSONDecodeError) as error:
            raise ValueError(f"{field} contains invalid JSON") from error

    @classmethod
    def _collection(cls, row: sqlite3.Row) -> dict[str, Any]:
        asset_ids = cls._json(row["asset_ids"], field="knowledge_datasets.asset_ids", default=[])
        capabilities = cls._json(row["capabilities"], field="knowledge_datasets.capabilities", default=[])
        freshness = cls._json(row["freshness"], field="knowledge_datasets.freshness", default={})
        semantic_asset_ids = cls._json(row["semantic_asset_ids"], field="knowledge_datasets.semantic_asset_ids", default=[])
        if (
            not isinstance(asset_ids, list)
            or not isinstance(semantic_asset_ids, list)
            or not isinstance(capabilities, list)
            or not isinstance(freshness, dict)
        ):
            raise ValueError("Collection JSON fields have an invalid shape")
        return {
            "id": str(row["id"]),
            "space_id": str(row["space_id"]),
            "name": str(row["name"]),
            "version": str(row["version"]),
            "kind": str(row["kind"]),
            "asset_ids": [str(item) for item in asset_ids],
            "semantic_asset_ids": [str(item) for item in semantic_asset_ids],
            "capabilities": [str(item) for item in capabilities],
            "freshness": freshness,
            "asset_count": len(asset_ids),
        }

    @classmethod
    def _collection_bindings(cls, connection: sqlite3.Connection) -> dict[tuple[str, str, str], dict[str, dict[str, str]]]:
        result: dict[tuple[str, str, str], dict[str, dict[str, str]]] = {}
        if cls._has_table(connection, "knowledge_collection_bindings"):
            for row in connection.execute(
                "SELECT space_id, collection_id, collection_version, capability, binding_json "
                "FROM knowledge_collection_bindings ORDER BY space_id, collection_id, collection_version, capability"
            ):
                binding = cls._json(row["binding_json"], field="knowledge_collection_bindings.binding_json", default={})
                if not isinstance(binding, dict) or set(binding) not in ({"asset_id"}, {"dataset_id"}, {"provider_id"}):
                    raise ValueError("Collection provider binding has an invalid shape")
                key = (str(row["space_id"]), str(row["collection_id"]), str(row["collection_version"]))
                capability = str(row["capability"])
                if capability in result.setdefault(key, {}):
                    raise ValueError("Collection provider binding is duplicated")
                result[key][capability] = {str(k): str(v) for k, v in binding.items()}
        # A derived query index must not replace ingestion ownership. Its
        # atomic active generation supplies the effective retrieval binding.
        if cls._has_table(connection, "knowledge_local_vector_indexes"):
            index_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(knowledge_local_vector_indexes)")}
            provider_select = ", provider_id" if "provider_id" in index_columns else ""
            seen = set()
            for row in connection.execute(
                "SELECT space_id,collection_id,collection_version,capability" + provider_select
                + " FROM knowledge_local_vector_indexes WHERE status='active'"
            ):
                key = (str(row["space_id"]), str(row["collection_id"]), str(row["collection_version"]))
                capability = str(row["capability"])
                if capability not in {"document_rag_query", "wiki_query"} or (*key, capability) in seen:
                    raise ValueError("Active vector binding is ambiguous")
                provider_id = (str(row["provider_id"])
                               if "provider_id" in index_columns else "knowledge_local_vector")
                if provider_id not in {"knowledge_local_vector", "knowledge_milvus_vector"}:
                    raise ValueError("Active vector provider is unknown")
                seen.add((*key, capability))
                result.setdefault(key, {})[capability] = {"provider_id": provider_id}
        return result

    @classmethod
    def _collection_with_bindings(
        cls,
        row: sqlite3.Row,
        bindings: dict[tuple[str, str, str], dict[str, dict[str, str]]],
    ) -> dict[str, Any]:
        collection = cls._collection(row)
        collection["provider_bindings"] = bindings.get(
            (collection["space_id"], collection["id"], collection["version"]),
        ) or {}
        return collection

    @staticmethod
    def _asset(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": str(row["id"]),
            "space_id": str(row["space_id"]),
            "kind": str(row["kind"]),
            "title": str(row["title"]),
            "description": str(row["description"] or ""),
            "mime_type": str(row["mime_type"]),
            "source_type": str(row["source_type"]),
            "source_uri": str(row["source_uri"]),
            "revision": str(row["revision"]),
            "content_digest": str(row["content_digest"]),
        }

    def list_collections(self, *, space_id: str | None = None) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            semantic_asset_column = "semantic_asset_ids" if self._has_column(connection, "knowledge_datasets", "semantic_asset_ids") else "NULL AS semantic_asset_ids"
            select = "SELECT id, space_id, name, version, kind, capabilities, freshness, asset_ids, " + semantic_asset_column
            if space_id is None:
                rows = connection.execute(
                    select + " FROM knowledge_datasets ORDER BY space_id, id, version"
                ).fetchall()
            else:
                rows = connection.execute(
                    select + " FROM knowledge_datasets WHERE space_id = ? ORDER BY id, version",
                    (space_id,),
                ).fetchall()
            bindings = self._collection_bindings(connection)
            return [self._collection_with_bindings(row, bindings) for row in rows]
        finally:
            connection.close()

    def list_connectors(self, *, space_id: str) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            if not self._has_table(connection, "knowledge_connectors"):
                return []
            rows = connection.execute(
                "SELECT id, space_id, connector_key, name, status, auth_type, last_sync_run_id, last_synced_at "
                "FROM knowledge_connectors WHERE space_id = ? ORDER BY id",
                (space_id,),
            ).fetchall()
            return [
                {
                    "id": str(row["id"]),
                    "space_id": str(row["space_id"]),
                    "connector_key": str(row["connector_key"]),
                    "name": str(row["name"] or ""),
                    "status": str(row["status"]),
                    "auth_type": str(row["auth_type"]),
                    "last_sync_run_id": str(row["last_sync_run_id"] or ""),
                    "last_synced_at": str(row["last_synced_at"] or ""),
                }
                for row in rows
            ]
        finally:
            connection.close()

    def list_source_items(self, *, space_id: str, connector_id: str | None = None) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            if not self._has_table(connection, "knowledge_source_items"):
                return []
            query = (
                "SELECT id, space_id, connector_id, external_type, title, revision, content_digest, asset_id, "
                "status, remote_created_at, remote_updated_at, last_seen_sync_run_id "
                "FROM knowledge_source_items WHERE space_id = ?"
            )
            parameters: list[Any] = [space_id]
            if connector_id is not None:
                query += " AND connector_id = ?"
                parameters.append(connector_id)
            query += " ORDER BY id"
            rows = connection.execute(query, parameters).fetchall()
            return [
                {
                    "id": str(row["id"]),
                    "space_id": str(row["space_id"]),
                    "connector_id": str(row["connector_id"]),
                    "external_type": str(row["external_type"]),
                    "title": str(row["title"] or ""),
                    "revision": str(row["revision"] or ""),
                    "content_digest": str(row["content_digest"] or ""),
                    "asset_id": str(row["asset_id"] or ""),
                    "status": str(row["status"]),
                    "remote_created_at": str(row["remote_created_at"] or ""),
                    "remote_updated_at": str(row["remote_updated_at"] or ""),
                    "last_seen_sync_run_id": str(row["last_seen_sync_run_id"] or ""),
                }
                for row in rows
            ]
        finally:
            connection.close()

    def search_assets(self, *, text: str, space_id: str | None, limit: int) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            query = (
                "SELECT id, space_id, kind, title, description, mime_type, source_type, source_uri, revision, content_digest "
                "FROM knowledge_assets WHERE (lower(title) LIKE ? ESCAPE '\\' OR lower(description) LIKE ? ESCAPE '\\')"
            )
            escaped = text.casefold().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = f"%{escaped}%"
            parameters: list[Any] = [pattern, pattern]
            if space_id is not None:
                query += " AND space_id = ?"
                parameters.append(space_id)
            query += " ORDER BY id LIMIT ?"
            parameters.append(limit)
            return [self._asset(row) for row in connection.execute(query, parameters).fetchall()]
        finally:
            connection.close()

    def list_assets(self, *, space_id: str | None = None) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            query = (
                "SELECT id, space_id, kind, title, description, mime_type, source_type, source_uri, revision, content_digest "
                "FROM knowledge_assets"
            )
            parameters: tuple[Any, ...] = ()
            if space_id is not None:
                query += " WHERE space_id = ?"
                parameters = (space_id,)
            query += " ORDER BY id"
            return [self._asset(row) for row in connection.execute(query, parameters).fetchall()]
        finally:
            connection.close()

    def list_spaces(self) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            rows = connection.execute("SELECT id, name, description FROM knowledge_spaces ORDER BY id").fetchall()
            return [
                {"id": str(row["id"]), "name": str(row["name"] or ""), "description": str(row["description"] or "")}
                for row in rows
            ]
        finally:
            connection.close()

    def list_semantic_assets(self) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            return self._semantic_assets_from_connection(connection)
        finally:
            connection.close()

    def get_asset(self, *, asset_id: str) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT id, space_id, kind, title, description, mime_type, source_type, source_uri, revision, content_digest "
                "FROM knowledge_assets WHERE id = ?",
                (asset_id,),
            ).fetchone()
            return self._asset(row) if row is not None else None
        finally:
            connection.close()

    def get_query_result(self, *, query_result_id: str) -> dict[str, Any] | None:
        """Read portable QueryResult metadata without opening its artifact."""

        connection = self._connect()
        try:
            if not self._has_table(connection, "knowledge_query_results"):
                return None
            row = connection.execute(
                "SELECT id, status, question, sql_digest, columns_json, row_count, profile_json, "
                "artifact_uri, artifact_reference_digest, artifact_format, correlation_json, created_at, expires_at "
                "FROM knowledge_query_results WHERE id = ?",
                (query_result_id,),
            ).fetchone()
            if row is None:
                return None
            columns = self._json(row["columns_json"], field="knowledge_query_results.columns_json", default=[])
            profile = self._json(row["profile_json"], field="knowledge_query_results.profile_json", default={})
            correlation = self._json(
                row["correlation_json"], field="knowledge_query_results.correlation_json", default={}
            )
            if not isinstance(columns, list) or not isinstance(profile, dict) or not isinstance(correlation, dict):
                raise ValueError("QueryResult JSON fields have an invalid shape")
            return {
                "id": str(row["id"]),
                "status": str(row["status"] or ""),
                "question": str(row["question"] or ""),
                "sql_digest": str(row["sql_digest"] or ""),
                "columns": [str(item) for item in columns],
                "row_count": int(row["row_count"] or 0),
                "profile": profile,
                "artifact_uri": str(row["artifact_uri"] or ""),
                "artifact_reference_digest": str(row["artifact_reference_digest"] or ""),
                "artifact_format": str(row["artifact_format"] or ""),
                "correlation": correlation,
                "created_at": str(row["created_at"] or ""),
                "expires_at": str(row["expires_at"] or ""),
            }
        finally:
            connection.close()

    @staticmethod
    def _job_timestamps(row: sqlite3.Row) -> dict[str, str]:
        return {
            key: str(row[key] or "")
            for key in ("created_at", "started_at", "finished_at")
        }

    def get_job(self, *, job_id: str) -> dict[str, Any] | None:
        """Read a redacted Admin job projection from the local Catalog.

        Job tables contain host paths, leases, arbitrary metadata and raw
        errors.  This projection deliberately selects only portable lifecycle
        fields; callers cannot request a table or column by URL input.
        """

        connection = self._connect()
        try:
            matching_tables = [
                table
                for table in (
                    "knowledge_processing_jobs",
                    "knowledge_authoring_jobs",
                    "knowledge_sync_runs",
                )
                if self._has_table(connection, table)
                and connection.execute(f"SELECT 1 FROM {table} WHERE id = ?", (job_id,)).fetchone() is not None
            ]
            if len(matching_tables) > 1:
                raise ValueError("Job identity is ambiguous across Catalog job tables")
            if self._has_table(connection, "knowledge_processing_jobs"):
                row = connection.execute(
                    "SELECT id, space_id, kind, status, asset_id, source_item_id, sync_run_id, "
                    "current_step, progress, retry_count, attempt, created_at, started_at, finished_at "
                    "FROM knowledge_processing_jobs WHERE id = ?",
                    (job_id,),
                ).fetchone()
                if row is not None:
                    return {
                        "id": str(row["id"]),
                        "kind": str(row["kind"] or ""),
                        "status": str(row["status"] or ""),
                        "space_id": str(row["space_id"] or ""),
                        "asset_id": str(row["asset_id"] or ""),
                        "source_item_id": str(row["source_item_id"] or ""),
                        "sync_run_id": str(row["sync_run_id"] or ""),
                        "current_step": str(row["current_step"] or ""),
                        "progress": int(row["progress"] or 0),
                        "retry_count": int(row["retry_count"] or 0),
                        "attempt": int(row["attempt"] or 0),
                        **self._job_timestamps(row),
                    }
            if self._has_table(connection, "knowledge_authoring_jobs"):
                row = connection.execute(
                    "SELECT id, kind, dimension_id, adapter, scope_uri, status, current_step, progress, "
                    "staging_uri, staging_reference_digest, published_uri, published_reference_digest, "
                    "retry_count, attempt, created_at, started_at, finished_at "
                    "FROM knowledge_authoring_jobs WHERE id = ?",
                    (job_id,),
                ).fetchone()
                if row is not None:
                    return {
                        "id": str(row["id"]),
                        "kind": str(row["kind"] or ""),
                        "status": str(row["status"] or ""),
                        "dimension_id": str(row["dimension_id"] or ""),
                        "adapter": str(row["adapter"] or ""),
                        "scope_uri": str(row["scope_uri"] or "") if str(row["scope_uri"] or "").startswith("knowledge://") else "",
                        "current_step": str(row["current_step"] or ""),
                        "progress": int(row["progress"] or 0),
                        "staging_uri": str(row["staging_uri"] or "") if str(row["staging_uri"] or "").startswith("knowledge://") else "",
                        "staging_reference_digest": str(row["staging_reference_digest"] or ""),
                        "published_uri": str(row["published_uri"] or "") if str(row["published_uri"] or "").startswith("knowledge://") else "",
                        "published_reference_digest": str(row["published_reference_digest"] or ""),
                        "retry_count": int(row["retry_count"] or 0),
                        "attempt": int(row["attempt"] or 0),
                        **self._job_timestamps(row),
                    }
            if self._has_table(connection, "knowledge_sync_runs"):
                row = connection.execute(
                    "SELECT id, connector_id, mode, status, current_step, progress, attempt, "
                    "created_at, started_at, finished_at FROM knowledge_sync_runs WHERE id = ?",
                    (job_id,),
                ).fetchone()
                if row is not None:
                    return {
                        "id": str(row["id"]),
                        "kind": "connector_sync",
                        "status": str(row["status"] or ""),
                        "connector_id": str(row["connector_id"] or ""),
                        "mode": str(row["mode"] or ""),
                        "current_step": str(row["current_step"] or ""),
                        "progress": int(row["progress"] or 0),
                        "attempt": int(row["attempt"] or 0),
                        **self._job_timestamps(row),
                    }
            return None
        finally:
            connection.close()

    def get_structured_asset(self, *, asset_id: str) -> dict[str, Any] | None:
        """Return Platform Structured Asset metadata, not a document Asset."""

        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT id, space_id, source_type, file_name, sheet_name, size_bytes, source_uri, content_digest, "
                "profile_status, row_count, column_count, columns_json, reference_status, capabilities, metadata_json "
                "FROM knowledge_structured_assets WHERE id = ?",
                (asset_id,),
            ).fetchone()
            if row is None:
                return None
            columns = self._json(row["columns_json"], field="knowledge_structured_assets.columns_json", default=[])
            capabilities = self._json(row["capabilities"], field="knowledge_structured_assets.capabilities", default=[])
            metadata = self._json(row["metadata_json"], field="knowledge_structured_assets.metadata_json", default={})
            if not isinstance(columns, list) or not isinstance(capabilities, list) or not isinstance(metadata, dict):
                raise ValueError("Structured Asset JSON fields have an invalid shape")
            result = {
                "id": str(row["id"]),
                "space_id": str(row["space_id"]),
                "kind": "structured_asset",
                "source_type": str(row["source_type"] or ""),
                "title": str(row["file_name"] or ""),
                "sheet_name": str(row["sheet_name"] or "") or None,
                "size_bytes": row["size_bytes"],
                "source_uri": str(row["source_uri"] or ""),
                "content_digest": str(row["content_digest"] or ""),
                "profile_status": str(row["profile_status"] or ""),
                "row_count": row["row_count"],
                "column_count": row["column_count"],
                "columns": [str(item) for item in columns],
                "reference_status": str(row["reference_status"] or ""),
                "capabilities": [str(item) for item in capabilities],
            }
            logical_dataset = metadata.get("logical_dataset")
            if isinstance(logical_dataset, dict):
                result["logical_dataset"] = logical_dataset
            return result
        finally:
            connection.close()

    def list_connector_authorizations(self, *, space_id: str) -> list[dict[str, Any]]:
        """Return only OAuth state metadata; never select credential refs or IDs."""

        connection = self._connect()
        try:
            if not self._has_table(connection, "knowledge_connectors"):
                return []
            has_grants = self._has_table(connection, "knowledge_credential_grants")
            has_sessions = self._has_table(connection, "knowledge_oauth_sessions")
            grant_select = (
                "COALESCE((SELECT MAX(status) FROM knowledge_credential_grants g "
                "WHERE g.connector_id = c.id), '') AS grant_status, "
                "COALESCE((SELECT COUNT(*) FROM knowledge_credential_grants g "
                "WHERE g.connector_id = c.id AND g.status = 'active'), 0) AS active_grant_count, "
                "COALESCE((SELECT MAX(updated_at) FROM knowledge_credential_grants g "
                "WHERE g.connector_id = c.id), '') AS grant_updated_at"
                if has_grants
                else "'' AS grant_status, 0 AS active_grant_count, '' AS grant_updated_at"
            )
            session_select = (
                "COALESCE((SELECT MAX(status) FROM knowledge_oauth_sessions s "
                "WHERE s.connector_id = c.id), '') AS oauth_session_status, "
                "COALESCE((SELECT MAX(expires_at) FROM knowledge_oauth_sessions s "
                "WHERE s.connector_id = c.id), '') AS oauth_session_expires_at"
                if has_sessions
                else "'' AS oauth_session_status, '' AS oauth_session_expires_at"
            )
            rows = connection.execute(
                "SELECT c.id, c.space_id, c.connector_key, c.name, c.status, c.auth_type, "
                f"{grant_select}, {session_select} "
                "FROM knowledge_connectors c WHERE c.space_id = ? ORDER BY c.id",
                (space_id,),
            ).fetchall()
            result = []
            for row in rows:
                grant_status = str(row["grant_status"] or "")
                session_status = str(row["oauth_session_status"] or "")
                active_grants = int(row["active_grant_count"] or 0)
                result.append(
                    {
                        "connector_id": str(row["id"]),
                        "space_id": str(row["space_id"]),
                        "connector_key": str(row["connector_key"]),
                        "name": str(row["name"] or ""),
                        "connector_status": str(row["status"] or ""),
                        "auth_type": str(row["auth_type"] or ""),
                        "grant_status": grant_status,
                        "active_grant_count": active_grants,
                        "oauth_session_status": session_status,
                        "authorization_required": active_grants == 0 and grant_status != "active",
                        "grant_updated_at": str(row["grant_updated_at"] or "") or None,
                        "oauth_session_expires_at": str(row["oauth_session_expires_at"] or "") or None,
                    }
                )
            return result
        finally:
            connection.close()
