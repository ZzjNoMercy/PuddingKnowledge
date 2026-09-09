from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import JSON, Column, DateTime, Integer, MetaData, String, Table, Text, create_engine, select


def _fixture(tmp_path: Path):
    source_engine = create_engine("sqlite:///:memory:")
    target_engine = create_engine("sqlite:///:memory:")
    metadata = MetaData()
    bases = Table(
        "knowledge_bases",
        metadata,
        Column("id", String(64), primary_key=True),
        Column("name", String(200), nullable=False),
        Column("description", Text, nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False),
        Column("updated_at", DateTime(timezone=True), nullable=False),
    )
    documents = Table(
        "knowledge_documents",
        metadata,
        Column("id", String(64), primary_key=True),
        Column("knowledge_base_id", String(64), nullable=False),
    )
    assets = Table(
        "knowledge_table_assets",
        metadata,
        Column("asset_id", String(64), primary_key=True),
        Column("knowledge_base_id", String(64), nullable=False),
        Column("document_id", String(64)),
        Column("source_type", String(40), nullable=False),
        Column("file_name", String(500), nullable=False),
        Column("storage_path", Text, nullable=False),
        Column("virtual_path", Text, nullable=False),
        Column("sheet_name", String(300)),
        Column("size_bytes", Integer, nullable=False),
        Column("modified_at", DateTime(timezone=True)),
        Column("content_sha256", String(64), nullable=False),
        Column("profile_status", String(40), nullable=False),
        Column("profile_path", Text, nullable=False),
        Column("rows", Integer),
        Column("columns_count", Integer),
        Column("columns", JSON, nullable=False),
        Column("reference_status", String(40), nullable=False),
        Column("asset_metadata", JSON, nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False),
        Column("updated_at", DateTime(timezone=True), nullable=False),
    )
    metadata.create_all(source_engine)
    moment = datetime(2026, 9, 3, 0, 5, tzinfo=timezone.utc)
    source_file = tmp_path / "sales.xlsx"
    profile_file = tmp_path / "sales.profile.json"
    source_file.write_text("xlsx-fixture", encoding="utf-8")
    profile_file.write_text('{"rows": 2}', encoding="utf-8")
    with source_engine.begin() as connection:
        connection.execute(
            bases.insert(),
            {"id": "kb_1", "name": "Sales", "description": "", "created_at": moment, "updated_at": moment},
        )
        connection.execute(documents.insert(), {"id": "doc_1", "knowledge_base_id": "kb_1"})
        connection.execute(
            assets.insert(),
            {
                "asset_id": "tbl_sales_2026",
                "knowledge_base_id": "kb_1",
                "document_id": "doc_1",
                "source_type": "uploaded",
                "file_name": "sales.xlsx",
                "storage_path": str(source_file),
                "virtual_path": "/knowledge/imported/sales.xlsx",
                "sheet_name": "Orders",
                "size_bytes": 12,
                "modified_at": moment,
                "content_sha256": "abc123",
                "profile_status": "ready",
                "profile_path": str(profile_file),
                "rows": 2,
                "columns_count": 2,
                "columns": ["brand", "sales"],
                "reference_status": "ready",
                "asset_metadata": {"api_key": "must-not-cross", "source": "https://example.test"},
                "created_at": moment,
                "updated_at": moment,
            },
        )
    return source_engine, target_engine, assets, source_file, profile_file


def test_structured_asset_catalog_rehearsal_copies_schema_profile_and_rolls_back(tmp_path: Path) -> None:
    from jsonschema import Draft202012Validator

    from knowledge_platform.catalog import (
        KnowledgeStructuredAsset,
        migrate_to_latest,
        run_structured_asset_catalog_rehearsal_with_rollback_probes,
    )

    source_engine, target_engine, _, source_file, profile_file = _fixture(tmp_path)
    moment = datetime(2026, 9, 3, 0, 5, tzinfo=timezone.utc)
    with target_engine.begin() as connection:
        migrate_to_latest(connection)
        connection.execute(
            KnowledgeStructuredAsset.__table__.insert().values(
                id="structured_other",
                space_id="space_other",
                source_key="tbl_other",
                document_asset_id=None,
                source_type="fixture",
                file_name="other.csv",
                sheet_name=None,
                size_bytes=1,
                modified_at=None,
                source_uri="knowledge://spaces/space_other/structured-assets/structured_other/source",
                source_reference_digest="sha256:other",
                logical_path_digest="sha256:other",
                profile_uri="",
                profile_reference_digest="",
                content_digest="sha256:other",
                profile_status="missing",
                row_count=None,
                column_count=None,
                columns_json=[],
                reference_status="ready",
                capabilities=["table_query"],
                metadata_json={"note": "other"},
                created_at=moment,
                updated_at=moment,
            )
        )
    with source_engine.connect() as source:
        result = run_structured_asset_catalog_rehearsal_with_rollback_probes(
            source,
            target_engine,
            installation_id="install-structured",
            source_revision="legacy-table-1",
            target_revision="platform-table-1",
            active_revision="legacy-active-1",
            file_reference_checker=lambda reference: Path(reference).exists(),
        )
    assert all(result.report.checks.values())
    assert result.report.retry_idempotent is True
    assert len(result.report.target_out_of_scope_tables) == 1
    with target_engine.connect() as connection:
        row = (
            connection.execute(
                select(KnowledgeStructuredAsset).where(KnowledgeStructuredAsset.id == "structured_tbl_sales_2026")
            )
            .mappings()
            .one()
        )
        serialized = json.dumps(dict(row), ensure_ascii=False, default=str)
        assert str(source_file) not in serialized and str(profile_file) not in serialized
        assert "must-not-cross" not in serialized and "https://example.test" not in serialized
        assert row["source_uri"].startswith("knowledge://")
        assert row["profile_uri"].startswith("knowledge://")
        assert row["columns_json"] == ["brand", "sales"]
    report_json = json.loads(json.dumps(result.to_dict(), ensure_ascii=False, default=str))
    schema = json.loads(
        (
            Path(__file__).resolve().parents[2] / "docs/knowledge-platform/catalog-rehearsal-report.schema.json"
        ).read_text()
    )
    assert list(Draft202012Validator(schema).iter_errors(report_json)) == []
    source_engine.dispose()
    target_engine.dispose()


def test_structured_asset_catalog_rehearsal_rejects_profile_count_drift(tmp_path: Path) -> None:
    from knowledge_platform.catalog import (
        RehearsalVerificationError,
        run_structured_asset_catalog_rehearsal_with_rollback_probes,
    )

    source_engine, target_engine, assets, _, _ = _fixture(tmp_path)
    with source_engine.begin() as connection:
        connection.execute(assets.update().values(columns_count=3))
    with source_engine.connect() as source:
        with pytest.raises(RehearsalVerificationError, match="columns_count mismatch"):
            run_structured_asset_catalog_rehearsal_with_rollback_probes(
                source,
                target_engine,
                installation_id="install-structured",
                source_revision="legacy-1",
                target_revision="platform-1",
                active_revision="legacy-active",
                file_reference_checker=lambda reference: Path(reference).exists(),
            )
    source_engine.dispose()
    target_engine.dispose()


def test_catalog_migration_rejects_drifted_structured_asset_schema() -> None:
    from knowledge_platform.catalog import KnowledgeStructuredAsset, migrate_to_latest

    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        assert migrate_to_latest(connection) == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]
        connection.exec_driver_sql("DROP INDEX ix_knowledge_structured_assets_space_status")
    with engine.begin() as connection:
        with pytest.raises(RuntimeError, match="schema drift.*knowledge_structured_assets"):
            migrate_to_latest(connection)
        assert KnowledgeStructuredAsset.__table__.name in connection.dialect.get_table_names(connection)
    engine.dispose()


def test_catalog_migration_rejects_catalog_tables_without_history() -> None:
    from knowledge_platform.catalog import KnowledgeSpace, migrate_to_latest

    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        KnowledgeSpace.__table__.create(connection)
    with engine.begin() as connection:
        with pytest.raises(RuntimeError, match="without schema history"):
            migrate_to_latest(connection)
    engine.dispose()


def test_catalog_migration_rejects_drifted_schema_history() -> None:
    from knowledge_platform.catalog import migrate_to_latest

    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE knowledge_catalog_schema_versions (version INTEGER)")
    with engine.begin() as connection:
        with pytest.raises(RuntimeError, match="schema drift.*knowledge_catalog_schema_versions"):
            migrate_to_latest(connection)
    engine.dispose()


def test_catalog_migration_rejects_malformed_pending_table_with_partial_history() -> None:
    from sqlalchemy import Column, Integer, MetaData, Table

    from knowledge_platform.catalog import (
        KnowledgeAsset,
        KnowledgeCatalogSchemaVersion,
        KnowledgeConnector,
        KnowledgeDataset,
        KnowledgeSourceItem,
        KnowledgeSpace,
        KnowledgeSyncRun,
        migrate_to_latest,
    )

    engine = create_engine("sqlite:///:memory:")
    malformed = Table("knowledge_structured_assets", MetaData(), Column("id", Integer, primary_key=True))
    with engine.begin() as connection:
        KnowledgeCatalogSchemaVersion.__table__.create(connection)
        for table in (
            KnowledgeSpace.__table__,
            KnowledgeAsset.__table__,
            KnowledgeDataset.__table__,
            KnowledgeConnector.__table__,
            KnowledgeSourceItem.__table__,
            KnowledgeSyncRun.__table__,
            malformed,
        ):
            table.create(connection)
        connection.execute(KnowledgeCatalogSchemaVersion.__table__.insert(), {"version": 1})
    with engine.begin() as connection:
        with pytest.raises(RuntimeError, match="schema drift.*knowledge_structured_assets"):
            migrate_to_latest(connection)
    engine.dispose()
