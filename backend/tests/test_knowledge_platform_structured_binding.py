from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from knowledge_contracts import Correlation, Principal
from knowledge_platform.catalog import SqliteCatalogQueryRepository, SqliteStructuredAssetWriter
from knowledge_platform.structured import (
    LocalStructuredFileBindingVerifier,
    StructuredAssetBindingRequest,
    StructuredAssetBindingService,
    StructuredSourceProfile,
)


def _digest(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _principal(*scopes: str, tenant_id: str | None = None) -> Principal:
    return Principal(subject_id="binding-test", tenant_id=tenant_id, scopes=tuple(scopes))


def _database(tmp_path: Path, *, status: str = "pending", source_type: str = "excel", content: bytes = b"name,total\nA,3\n") -> tuple[Path, str, str]:
    database = tmp_path / "catalog.sqlite3"
    digest = _digest(content)
    source_id = "structured_tbl_sales"
    space_id = "space_sales"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE knowledge_structured_assets (
                id TEXT PRIMARY KEY, space_id TEXT NOT NULL, source_key TEXT NOT NULL,
                document_asset_id TEXT, source_type TEXT NOT NULL, file_name TEXT NOT NULL,
                sheet_name TEXT, size_bytes INTEGER NOT NULL, modified_at TEXT, source_uri TEXT NOT NULL,
                source_reference_digest TEXT NOT NULL, logical_path_digest TEXT NOT NULL,
                profile_uri TEXT NOT NULL, profile_reference_digest TEXT NOT NULL, content_digest TEXT NOT NULL,
                profile_status TEXT NOT NULL, row_count INTEGER, column_count INTEGER NOT NULL,
                columns_json TEXT NOT NULL, reference_status TEXT NOT NULL, capabilities TEXT NOT NULL,
                metadata_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            """
        )
        connection.execute(
            """
            INSERT INTO knowledge_structured_assets VALUES
            (?, ?, ?, NULL, ?, 'sales.csv', NULL, ?, NULL, ?, '', '', '', '', ?, 'ready', 1, 2,
             '[\"name\", \"total\"]', ?, '[\"table_query\"]', '{}', 'now', 'now')
            """,
            (
                source_id,
                space_id,
                source_id,
                source_type,
                len(content),
                f"knowledge://spaces/{space_id}/structured-assets/{source_id}/source",
                digest,
                status,
            ),
        )
    return database, source_id, space_id


def test_binding_verifies_bytes_and_publishes_pending_source_without_path(tmp_path: Path) -> None:
    content = b"name,total\nA,3\n"
    database, asset_id, space_id = _database(tmp_path, content=content)
    source = tmp_path / "sales.csv"
    source.write_bytes(content)
    repository = SqliteCatalogQueryRepository(database)
    result = StructuredAssetBindingService(
        catalog=repository,
        verifier=LocalStructuredFileBindingVerifier(),
        writer=SqliteStructuredAssetWriter(database),
    ).bind(
        principal=_principal("knowledge:processing", f"knowledge:space:{space_id}"),
        correlation=Correlation("trace-source-binding"),
        request=StructuredAssetBindingRequest(asset_id=asset_id, space_id=space_id, path=source),
    )

    assert result.status == "ok"
    stored = repository.get_structured_asset(asset_id=asset_id)
    assert stored is not None
    assert stored["reference_status"] == "ready"
    assert stored["content_digest"] == _digest(content)
    assert stored["size_bytes"] == len(content)
    assert "path" not in str(result.data)
    assert result.evidence[0].matched_by == ("source_digest", "explicit_path_binding")


def test_binding_rejects_tenant_and_wrong_bytes_and_is_one_shot(tmp_path: Path) -> None:
    content = b"name,total\nA,3\n"
    database, asset_id, space_id = _database(tmp_path, content=content)
    source = tmp_path / "wrong.csv"
    source.write_bytes(b"name,total\nA,4\n")
    service = StructuredAssetBindingService(
        catalog=SqliteCatalogQueryRepository(database),
        verifier=LocalStructuredFileBindingVerifier(),
        writer=SqliteStructuredAssetWriter(database),
    )
    denied = service.bind(
        principal=_principal("knowledge:processing", f"knowledge:space:{space_id}", tenant_id="tenant-a"),
        correlation=Correlation("trace-binding-denied"),
        request=StructuredAssetBindingRequest(asset_id=asset_id, space_id=space_id, path=source),
    )
    assert denied.status == "error"
    assert denied.error is not None and denied.error.code.value == "permission_denied"

    mismatch = service.bind(
        principal=_principal("knowledge:processing", f"knowledge:space:{space_id}"),
        correlation=Correlation("trace-binding-mismatch"),
        request=StructuredAssetBindingRequest(asset_id=asset_id, space_id=space_id, path=source),
    )
    assert mismatch.status == "error"
    assert mismatch.error is not None and mismatch.error.code.value == "binding_unavailable"

    source.write_bytes(content)
    success = service.bind(
        principal=_principal("knowledge:processing", f"knowledge:space:{space_id}"),
        correlation=Correlation("trace-binding-success"),
        request=StructuredAssetBindingRequest(asset_id=asset_id, space_id=space_id, path=source),
    )
    assert success.status == "ok"
    repeat = service.bind(
        principal=_principal("knowledge:processing", f"knowledge:space:{space_id}"),
        correlation=Correlation("trace-binding-repeat"),
        request=StructuredAssetBindingRequest(asset_id=asset_id, space_id=space_id, path=source),
    )
    assert repeat.status == "error"
    assert repeat.error is not None and repeat.error.code.value == "binding_unavailable"


def test_binding_rejects_logical_dataset_even_with_matching_bytes(tmp_path: Path) -> None:
    content = b"name,total\nA,3\n"
    database, asset_id, space_id = _database(tmp_path, source_type="logical_concat", content=content)
    source = tmp_path / "sales.csv"
    source.write_bytes(content)
    result = StructuredAssetBindingService(
        catalog=SqliteCatalogQueryRepository(database),
        verifier=LocalStructuredFileBindingVerifier(),
        writer=SqliteStructuredAssetWriter(database),
    ).bind(
        principal=_principal("knowledge:processing", f"knowledge:space:{space_id}"),
        correlation=Correlation("trace-logical-binding"),
        request=StructuredAssetBindingRequest(asset_id=asset_id, space_id=space_id, path=source),
    )
    assert result.status == "error"
    assert result.error is not None and result.error.code.value == "binding_unavailable"


def test_writer_rejects_path_like_profile_columns(tmp_path: Path) -> None:
    content = b"name,total\nA,3\n"
    database, asset_id, space_id = _database(tmp_path, content=content)
    writer = SqliteStructuredAssetWriter(database)
    profile = StructuredSourceProfile(
        columns=("/Users/pet/private-column",),
        row_count=1,
        content_digest=_digest(content),
    )
    try:
        writer.bind_source_asset(
            principal=_principal("knowledge:processing", f"knowledge:space:{space_id}"),
            asset_id=asset_id,
            space_id=space_id,
            expected_content_digest=_digest(content),
            profile=profile,
        )
    except ValueError as error:
        assert str(error) == "source binding columns are unsafe"
    else:
        raise AssertionError("path-like profile columns must be rejected")


def test_collection_provider_binding_is_persisted_and_read_back(tmp_path: Path) -> None:
    database = tmp_path / "catalog.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE knowledge_datasets (
                id TEXT NOT NULL, space_id TEXT NOT NULL, name TEXT NOT NULL,
                version TEXT NOT NULL, kind TEXT NOT NULL, capabilities TEXT NOT NULL,
                freshness TEXT NOT NULL, asset_ids TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY (space_id, id, version)
            );
            CREATE TABLE knowledge_collection_bindings (
                space_id TEXT NOT NULL, collection_id TEXT NOT NULL, collection_version TEXT NOT NULL,
                capability TEXT NOT NULL, binding_json TEXT NOT NULL, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (space_id, collection_id, collection_version, capability)
            );
            CREATE TABLE knowledge_structured_assets (
                id TEXT PRIMARY KEY, space_id TEXT NOT NULL, reference_status TEXT NOT NULL,
                capabilities TEXT NOT NULL
            );
            INSERT INTO knowledge_datasets VALUES
                ('dataset_sales', 'space_sales', 'Sales', 'v1', 'document-rag',
                 '["document_rag_query"]', '{}', '[]', 'now');
            INSERT INTO knowledge_structured_assets VALUES
                ('structured_tbl_sales', 'space_sales', 'ready', '["table_query"]');
            """
        )
    writer = SqliteStructuredAssetWriter(database)
    stored = writer.bind_collection_provider(
        principal=_principal("knowledge:processing", "knowledge:space:space_sales"),
        collection_id="dataset_sales",
        collection_version="v1",
        space_id="space_sales",
        capability="table_query",
        binding={"asset_id": "structured_tbl_sales"},
    )
    assert stored["binding"] == {"asset_id": "structured_tbl_sales"}
    row = SqliteCatalogQueryRepository(database).list_collections(space_id="space_sales")[0]
    assert row["capabilities"] == ["document_rag_query", "table_query"]
    assert row["asset_ids"] == ["structured_tbl_sales"]
    assert row["provider_bindings"] == {"table_query": {"asset_id": "structured_tbl_sales"}}


def test_document_provider_binding_is_persisted_without_becoming_an_asset(tmp_path: Path) -> None:
    database = tmp_path / "catalog.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE knowledge_datasets (
                id TEXT NOT NULL, space_id TEXT NOT NULL, name TEXT NOT NULL,
                version TEXT NOT NULL, kind TEXT NOT NULL, capabilities TEXT NOT NULL,
                freshness TEXT NOT NULL, asset_ids TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY (space_id, id, version)
            );
            CREATE TABLE knowledge_collection_bindings (
                space_id TEXT NOT NULL, collection_id TEXT NOT NULL, collection_version TEXT NOT NULL,
                capability TEXT NOT NULL, binding_json TEXT NOT NULL, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (space_id, collection_id, collection_version, capability)
            );
            INSERT INTO knowledge_datasets VALUES
                ('dataset_docs', 'space_docs', 'Docs', 'v1', 'document-rag',
                 '["document_rag_query"]', '{}', '["asset-a"]', 'now');
            """
        )
    writer = SqliteStructuredAssetWriter(database)
    stored = writer.bind_collection_provider(
        principal=_principal("knowledge:processing", "knowledge:space:space_docs"),
        collection_id="dataset_docs",
        collection_version="v1",
        space_id="space_docs",
        capability="document_rag_query",
        binding={"provider_id": "candidate-a"},
    )
    assert stored["binding"] == {"provider_id": "candidate-a"}
    row = SqliteCatalogQueryRepository(database).list_collections(space_id="space_docs")[0]
    assert row["asset_ids"] == ["asset-a"]
    assert row["provider_bindings"] == {"document_rag_query": {"provider_id": "candidate-a"}}
