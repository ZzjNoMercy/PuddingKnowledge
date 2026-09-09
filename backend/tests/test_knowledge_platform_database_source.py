from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import JSON, Column, DateTime, Integer, MetaData, String, Table, Text, create_engine, select


def _fixture():
    source_engine = create_engine("sqlite:///:memory:")
    target_engine = create_engine("sqlite:///:memory:")
    metadata = MetaData()
    bases = Table(
        "knowledge_bases",
        metadata,
        Column("id", String(64), primary_key=True),
    )
    sources = Table(
        "knowledge_database_sources",
        metadata,
        Column("id", String(64), primary_key=True),
        Column("knowledge_base_id", String(64), nullable=False),
        Column("source_type", String(40), nullable=False),
        Column("name", String(200), nullable=False),
        Column("description", Text, nullable=False),
        Column("host", String(300), nullable=False),
        Column("port", Integer, nullable=False),
        Column("database", String(200), nullable=False),
        Column("username", String(200), nullable=False),
        Column("password", Text, nullable=False),
        Column("selected_tables", JSON, nullable=False),
        Column("source_metadata", JSON, nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False),
        Column("updated_at", DateTime(timezone=True), nullable=False),
    )
    metadata.create_all(source_engine)
    moment = datetime(2026, 9, 3, 0, 5, tzinfo=timezone.utc)
    with source_engine.begin() as connection:
        connection.execute(bases.insert(), {"id": "kb_1"})
        connection.execute(
            sources.insert(),
            {
                "id": "dbs_1",
                "knowledge_base_id": "kb_1",
                "source_type": "postgresql",
                "name": "Sales DB",
                "description": "Reporting database",
                "host": "db.internal",
                "port": 5432,
                "database": "sales",
                "username": "reporter",
                "password": "super-secret-password",
                "selected_tables": ["public.orders", "public.customers"],
                "source_metadata": {"builtin": False, "sslmode": "verify-full"},
                "created_at": moment,
                "updated_at": moment,
            },
        )
    return source_engine, target_engine, sources


def test_database_source_rehearsal_splits_secret_and_proves_rollback() -> None:
    from knowledge_platform.catalog import (
        KnowledgeDatabaseSource,
        run_database_source_catalog_rehearsal_with_rollback_probes,
    )

    source_engine, target_engine, _ = _fixture()
    from knowledge_platform.catalog.database_source_rehearsal import _source_rows

    with source_engine.connect() as connection:
        source_rows = _source_rows(connection)
    assert "password" not in source_rows[0]
    assert "super-secret-password" not in json.dumps(source_rows, default=str)
    from knowledge_platform.catalog import KnowledgeSpace, migrate_to_latest

    with target_engine.begin() as connection:
        migrate_to_latest(connection)
        connection.execute(
            KnowledgeSpace.__table__.insert().values(
                id="space_kb_1",
                name="Sales",
                description="",
                permissions_json={},
                created_at=datetime(2026, 9, 3, 0, 5, tzinfo=timezone.utc),
                updated_at=datetime(2026, 9, 3, 0, 5, tzinfo=timezone.utc),
            )
        )
    with source_engine.connect() as source:
        result = run_database_source_catalog_rehearsal_with_rollback_probes(
            source,
            target_engine,
            installation_id="install-database-source",
            source_revision="legacy-db-source-1",
            target_revision="platform-db-source-1",
            active_revision="legacy-active-1",
        )
    assert result.report.retry_idempotent is True
    assert all(result.report.checks.values())
    assert result.report.injected_failure_checkpoints == (
        "after_schema",
        "after_database_sources",
        "before_verification",
    )
    with target_engine.connect() as connection:
        row = connection.execute(select(KnowledgeDatabaseSource)).mappings().one()
    serialized = json.dumps(dict(row), ensure_ascii=False, default=str)
    assert "super-secret-password" not in serialized
    assert row["credential_ref"].startswith("vault://")
    assert row["config_json"] == {"builtin": False, "sslmode": "verify-full", "migration_state": "pending_rebind"}
    assert row["selected_tables"] == ["public.orders", "public.customers"]
    report_json = json.loads(json.dumps(result.to_dict(), ensure_ascii=False, default=str))
    schema_path = Path(__file__).resolve().parents[2] / "docs/knowledge-platform/catalog-rehearsal-report.schema.json"
    from jsonschema import Draft202012Validator

    assert list(Draft202012Validator(json.loads(schema_path.read_text())).iter_errors(report_json)) == []
    source_engine.dispose()
    target_engine.dispose()


def test_database_source_rehearsal_redacts_preexisting_out_of_scope_rows() -> None:
    from knowledge_platform.catalog import (
        KnowledgeDatabaseSource,
        KnowledgeSpace,
        migrate_to_latest,
        run_database_source_catalog_rehearsal_with_rollback_probes,
    )
    from knowledge_platform.catalog.database_source_rehearsal import _sanitize_path_value
    from knowledge_platform.catalog.rehearsal import build_table_snapshot
    from knowledge_platform.catalog.rehearsal_runner import _json_safe

    source_engine, target_engine, _ = _fixture()
    with target_engine.begin() as connection:
        migrate_to_latest(connection)
        connection.execute(
            KnowledgeSpace.__table__.insert().values(
                id="space_kb_1",
                name="Sales",
                description="",
                permissions_json={},
                created_at=datetime(2026, 9, 3, 0, 5, tzinfo=timezone.utc),
                updated_at=datetime(2026, 9, 3, 0, 5, tzinfo=timezone.utc),
            )
        )
        other = {
            "id": "dbsource_other",
            "space_id": "space_kb_1",
            "source_key": "other",
            "source_type": "postgresql",
            "name": "Other",
            "description": "",
            "host": "db.internal",
            "port": 5432,
            "database_name": "other",
            "username": "reporter",
            "credential_ref": "vault://users/local/credentials/database-source-other",
            "selected_tables": [],
            "config_json": {"password": "out-of-scope-secret"},
            "created_at": datetime(2026, 9, 3, 0, 5, tzinfo=timezone.utc),
            "updated_at": datetime(2026, 9, 3, 0, 5, tzinfo=timezone.utc),
        }
        connection.execute(KnowledgeDatabaseSource.__table__.insert().values(**other))
    with source_engine.connect() as source:
        result = run_database_source_catalog_rehearsal_with_rollback_probes(
            source,
            target_engine,
            installation_id="install-db",
            source_revision="legacy-db-1",
            target_revision="platform-db-1",
            active_revision="legacy-active",
        )
    with target_engine.connect() as connection:
        stored_other = dict(
            connection.execute(
                select(KnowledgeDatabaseSource).where(KnowledgeDatabaseSource.id == "dbsource_other")
            ).mappings().one()
        )
    assert len(result.report.target_out_of_scope_tables) == 1
    expected = build_table_snapshot(
        "knowledge_database_connectors:out_of_scope",
        [_json_safe(_sanitize_path_value(stored_other))],
        primary_key_fields=("id",),
        secret_fields=("credential_ref",),
    )
    assert result.report.target_out_of_scope_tables[0] == expected
    assert "out-of-scope-secret" not in json.dumps(result.to_dict(), default=str)
    source_engine.dispose()
    target_engine.dispose()


def test_database_source_rehearsal_rejects_unsafe_credential_ref() -> None:
    from knowledge_platform.catalog import (
        RehearsalVerificationError,
        run_database_source_catalog_rehearsal_with_rollback_probes,
    )

    source_engine, target_engine, sources = _fixture()
    with source_engine.begin() as connection:
        connection.execute(sources.update().values(password="", source_metadata={"credential_ref": "plain-secret"}))
    with source_engine.connect() as source:
        with pytest.raises(RehearsalVerificationError, match="unsafe credential reference"):
            run_database_source_catalog_rehearsal_with_rollback_probes(
                source,
                target_engine,
                installation_id="install-db",
                source_revision="legacy-db-1",
                target_revision="platform-db-1",
                active_revision="legacy-active-1",
                failure_checkpoints=("after_schema",),
            )
    source_engine.dispose()
    target_engine.dispose()


def test_database_source_rehearsal_rejects_unknown_metadata_key() -> None:
    from knowledge_platform.catalog import (
        RehearsalVerificationError,
        run_database_source_catalog_rehearsal_with_rollback_probes,
    )

    source_engine, target_engine, sources = _fixture()
    with source_engine.begin() as connection:
        connection.execute(sources.update().values(source_metadata={"x": "p@ss-91f7c0e9"}))
    with source_engine.connect() as source:
        with pytest.raises(RehearsalVerificationError, match="unsupported keys"):
            run_database_source_catalog_rehearsal_with_rollback_probes(
                source,
                target_engine,
                installation_id="install-db",
                source_revision="legacy-db-1",
                target_revision="platform-db-1",
                active_revision="legacy-active",
                failure_checkpoints=("after_schema",),
            )
    source_engine.dispose()
    target_engine.dispose()


def test_database_source_rehearsal_rejects_cross_source_credential_ref() -> None:
    from knowledge_platform.catalog import (
        RehearsalVerificationError,
        run_database_source_catalog_rehearsal_with_rollback_probes,
    )

    source_engine, target_engine, sources = _fixture()
    with source_engine.begin() as connection:
        connection.execute(
            sources.update().values(
                password="",
                source_metadata={"credential_ref": "vault://users/local/credentials/database-source-other"},
            )
        )
    with source_engine.connect() as source:
        with pytest.raises(RehearsalVerificationError, match="identity mismatch"):
            run_database_source_catalog_rehearsal_with_rollback_probes(
                source,
                target_engine,
                installation_id="install-db",
                source_revision="legacy-db-1",
                target_revision="platform-db-1",
                active_revision="legacy-active",
                failure_checkpoints=("after_schema",),
            )
    source_engine.dispose()
    target_engine.dispose()


def test_database_source_rehearsal_rejects_orphan_and_duplicate_selected_tables() -> None:
    from knowledge_platform.catalog import (
        RehearsalVerificationError,
        run_database_source_catalog_rehearsal_with_rollback_probes,
    )

    source_engine, target_engine, sources = _fixture()
    with source_engine.begin() as connection:
        connection.execute(sources.update().values(selected_tables=["public.orders", "public.orders"]))
    with source_engine.connect() as source:
        with pytest.raises(RehearsalVerificationError, match="duplicate selected table"):
            run_database_source_catalog_rehearsal_with_rollback_probes(
                source,
                target_engine,
                installation_id="install-db",
                source_revision="legacy-db-1",
                target_revision="platform-db-1",
                active_revision="legacy-active-1",
                failure_checkpoints=("after_schema",),
            )
    with source_engine.begin() as connection:
        connection.execute(sources.update().values(selected_tables=["public.orders"]))
        connection.execute(sources.update().values(knowledge_base_id="kb_missing"))
    with source_engine.connect() as source:
        with pytest.raises(RehearsalVerificationError, match="missing knowledge base"):
            run_database_source_catalog_rehearsal_with_rollback_probes(
                source,
                target_engine,
                installation_id="install-db",
                source_revision="legacy-db-1",
                target_revision="platform-db-1",
                active_revision="legacy-active-1",
                failure_checkpoints=("after_schema",),
            )
    source_engine.dispose()
    target_engine.dispose()
