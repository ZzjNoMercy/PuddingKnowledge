from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from knowledge_contracts import Correlation, Principal, QueryErrorCode
from knowledge_platform.catalog import CatalogQueryService, SqliteCatalogQueryRepository


def _catalog(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE knowledge_spaces (id TEXT PRIMARY KEY, name TEXT, description TEXT);
        CREATE TABLE knowledge_assets (
            id TEXT PRIMARY KEY, space_id TEXT, kind TEXT, title TEXT, description TEXT,
            mime_type TEXT, source_type TEXT, source_uri TEXT, revision TEXT, content_digest TEXT
        );
        CREATE TABLE knowledge_datasets (
            id TEXT, space_id TEXT, name TEXT, version TEXT, kind TEXT,
            capabilities TEXT, freshness TEXT, asset_ids TEXT
        );
        INSERT INTO knowledge_spaces VALUES ('space_1', 'Local', 'Local Markdown exposed as /knowledge/.');
        INSERT INTO knowledge_assets VALUES
            ('asset_1', 'space_1', 'document', 'Alpha Guide', 'vehicle notes', 'text/plain', 'local',
             'knowledge://spaces/space_1/assets/asset_1', 'sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
             'sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb');
        INSERT INTO knowledge_datasets VALUES
            ('collection_1', 'space_1', 'Local Collection', 'v1', 'document-rag',
             '["knowledge_list","knowledge_search","knowledge_read"]',
             '{"mode":"snapshot","source_revision":"local-1"}', '["asset_1"]');
        """
    )
    connection.commit()
    connection.close()


def test_catalog_query_service_lists_searches_and_reads_without_writes(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite3"
    _catalog(path)
    service = CatalogQueryService(SqliteCatalogQueryRepository(path))
    principal = Principal("tester", scopes=("knowledge.list", "knowledge.search", "knowledge.read"))
    correlation = Correlation("trace-1")
    before = hashlib.sha256(path.read_bytes()).hexdigest()

    collections = service.list_collections(principal=principal, correlation=correlation)
    search = service.search_assets(principal=principal, correlation=correlation, text="alpha")
    asset = service.read_asset(principal=principal, correlation=correlation, asset_id="asset_1")
    spaces = service.list_spaces(principal=principal, correlation=correlation)

    assert collections.status == "ok"
    assert spaces.status == "ok"
    assert spaces.data["spaces"][0]["description"] == ""
    assert collections.data["collections"][0]["name"] == "Local Collection"
    assert search.data["count"] == 1
    assert search.evidence[0].asset_id == "asset_1"
    assert search.evidence[0].matched_by == ("title",)
    assert asset.data["asset"]["title"] == "Alpha Guide"
    assert asset.provenance is not None and asset.provenance.capability == "knowledge_read"
    assert asset.provenance is not None and asset.provenance.catalog_revision.startswith("sha256:")
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_catalog_query_service_reads_portable_query_result_metadata(tmp_path: Path) -> None:
    path = tmp_path / "catalog-with-query-result.sqlite3"
    _catalog(path)
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE knowledge_query_results (
            id TEXT PRIMARY KEY, status TEXT, question TEXT, sql_digest TEXT,
            columns_json TEXT, row_count INTEGER, profile_json TEXT,
            artifact_uri TEXT, artifact_reference_digest TEXT, artifact_format TEXT,
            correlation_json TEXT, created_at TEXT, expires_at TEXT
        )
        """
    )
    connection.execute(
        "INSERT INTO knowledge_query_results VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "query_result_qr_1", "ready", "sales?", "sha256:" + "a" * 64,
            '["brand", "sales"]', 2, '{"rows_digest":"sha256:' + "b" * 64 + '"}',
            "knowledge://query-results/query_result_qr_1/artifact", "sha256:" + "c" * 64,
            "jsonl", '{"session_id_digest":"sha256:' + "d" * 64 + '"}',
            "2026-09-05T00:00:00+00:00", "2026-09-12T00:00:00+00:00",
        ),
    )
    connection.commit()
    connection.close()

    service = CatalogQueryService(SqliteCatalogQueryRepository(path))
    result = service.read_query_result(
        principal=Principal("tester", scopes=("knowledge.read",)),
        correlation=Correlation("trace-query-result"),
        query_result_id="query_result_qr_1",
    )

    assert result.status == "ok"
    payload = result.data["query_result"]
    assert payload["columns"] == ("brand", "sales")
    assert payload["artifact_uri"].startswith("knowledge://")
    assert "sql" not in payload and "artifact_path" not in payload


def test_catalog_query_service_query_result_is_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "catalog-with-query-result.sqlite3"
    _catalog(path)
    service = CatalogQueryService(SqliteCatalogQueryRepository(path))
    result = service.read_query_result(
        principal=Principal("tester", scopes=("knowledge.read",)),
        correlation=Correlation("trace-query-result-missing"),
        query_result_id="missing",
    )
    assert result.error is not None and result.error.code == QueryErrorCode.NOT_FOUND


def test_sqlite_package_snapshot_preserves_semantic_assets(tmp_path: Path) -> None:
    path = tmp_path / "catalog-with-semantics.sqlite3"
    _catalog(path)
    connection = sqlite3.connect(path)
    connection.execute("ALTER TABLE knowledge_datasets ADD COLUMN semantic_asset_ids TEXT")
    connection.execute("UPDATE knowledge_datasets SET semantic_asset_ids = ?", ('["measure:sales"]',))
    connection.execute(
        """
        CREATE TABLE knowledge_semantic_assets (
            id TEXT, space_id TEXT, type TEXT, name TEXT, description TEXT,
            aliases TEXT, tags TEXT, frontmatter TEXT, body TEXT
        )
        """
    )
    connection.execute(
        "INSERT INTO knowledge_semantic_assets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "measure:sales",
            "space_1",
            "measure",
            "Sales",
            "Revenue",
            '["revenue"]',
            '["finance"]',
            '{"unit":"CNY"}',
            "# Sales\n\nRevenue measure.",
        ),
    )
    connection.commit()
    connection.close()

    snapshot = SqliteCatalogQueryRepository(path).read_package_snapshot()

    assert snapshot.collections[0]["semantic_asset_ids"] == ["measure:sales"]
    assert snapshot.semantic_assets[0]["id"] == "measure:sales"
    assert snapshot.semantic_assets[0]["body"].startswith("# Sales")


def test_catalog_query_service_fails_closed_on_scope_and_input(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite3"
    _catalog(path)
    service = CatalogQueryService(SqliteCatalogQueryRepository(path))
    correlation = Correlation("trace-2")

    denied = service.list_collections(principal=Principal("tester"), correlation=correlation)
    invalid = service.search_assets(
        principal=Principal("tester", scopes=("knowledge.search",)), correlation=correlation, text="", limit=0
    )
    missing = service.read_asset(
        principal=Principal("tester", scopes=("knowledge.read",)), correlation=correlation, asset_id="missing"
    )

    assert denied.error is not None and denied.error.code == QueryErrorCode.PERMISSION_DENIED
    assert invalid.error is not None and invalid.error.code == QueryErrorCode.INVALID_REQUEST
    assert missing.error is not None and missing.error.code == QueryErrorCode.NOT_FOUND
    json.dumps(denied.to_dict(), ensure_ascii=False)


def test_catalog_query_service_rejects_empty_space_and_repository_failures() -> None:
    class BrokenRepository:
        @property
        def catalog_revision(self):
            return "sha256:" + "c" * 64

        def list_collections(self, *, space_id: str | None = None):
            raise RuntimeError("database unavailable")

        def search_assets(self, *, text: str, space_id: str | None, limit: int):
            raise RuntimeError("database unavailable")

        def get_asset(self, *, asset_id: str):
            raise RuntimeError("database unavailable")

    service = CatalogQueryService(BrokenRepository())
    principal = Principal("tester", scopes=("knowledge.list", "knowledge.search", "knowledge.read"))
    correlation = Correlation("trace-3")

    invalid = service.search_assets(principal=principal, correlation=correlation, text="x", space_id=" ")
    listed = service.list_collections(principal=principal, correlation=correlation)
    searched = service.search_assets(principal=principal, correlation=correlation, text="x")
    read = service.read_asset(principal=principal, correlation=correlation, asset_id="asset_1")
    tenant_denied = service.read_asset(
        principal=Principal("tenant-user", tenant_id="tenant-a", scopes=("knowledge.read",)),
        correlation=correlation,
        asset_id="asset_1",
    )

    assert invalid.error is not None and invalid.error.code == QueryErrorCode.INVALID_REQUEST
    assert listed.error is not None and listed.error.code == QueryErrorCode.INTERNAL_ERROR
    assert searched.error is not None and searched.error.code == QueryErrorCode.INTERNAL_ERROR
    assert read.error is not None and read.error.code == QueryErrorCode.INTERNAL_ERROR
    assert tenant_denied.error is not None and tenant_denied.error.code == QueryErrorCode.PERMISSION_DENIED


def test_catalog_query_service_rejects_mismatched_search_evidence() -> None:
    class MismatchedRepository:
        @property
        def catalog_revision(self):
            return "sha256:" + "d" * 64

        def search_assets(self, *, text: str, space_id: str | None, limit: int):
            return [
                {
                    "id": "asset_1",
                    "space_id": "space_1",
                    "title": "unrelated",
                    "description": "also unrelated",
                    "source_uri": "knowledge://spaces/space_1/assets/asset_1",
                    "revision": "",
                }
            ]

    result = CatalogQueryService(MismatchedRepository()).search_assets(
        principal=Principal("tester", scopes=("knowledge.search",)),
        correlation=Correlation("trace-4"),
        text="requested",
    )

    assert result.error is not None and result.error.code == QueryErrorCode.INTERNAL_ERROR


def test_catalog_query_service_fails_closed_when_revision_is_missing() -> None:
    class LegacyRepository:
        def list_collections(self, *, space_id: str | None = None):
            return []

        def search_assets(self, *, text: str, space_id: str | None, limit: int):
            return []

        def get_asset(self, *, asset_id: str):
            return None

    result = CatalogQueryService(LegacyRepository()).list_collections(
        principal=Principal("tester", scopes=("knowledge.list",)),
        correlation=Correlation("trace-5"),
    )

    assert result.error is not None and result.error.code == QueryErrorCode.INTERNAL_ERROR


def test_catalog_query_service_fails_closed_when_revision_read_fails() -> None:
    class BrokenRevisionRepository:
        @property
        def catalog_revision(self):
            raise OSError("sidecar disappeared")

    service = CatalogQueryService(BrokenRevisionRepository())
    result = service.list_collections(
        principal=Principal("tester", scopes=("knowledge.list",)),
        correlation=Correlation("trace-6"),
    )

    assert result.error is not None and result.error.code == QueryErrorCode.INTERNAL_ERROR
