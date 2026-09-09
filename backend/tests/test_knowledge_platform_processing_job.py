from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import JSON, Column, DateTime, Integer, MetaData, String, Table, Text, create_engine, select


def _fixture(tmp_path: Path):
    source_engine = create_engine("sqlite:///:memory:")
    target_engine = create_engine("sqlite:///:memory:")
    metadata = MetaData()
    bases = Table("knowledge_bases", metadata, Column("id", String(64), primary_key=True))
    documents = Table(
        "knowledge_documents",
        metadata,
        Column("id", String(64), primary_key=True),
        Column("knowledge_base_id", String(64), nullable=False),
    )
    connections = Table(
        "knowledge_source_connections",
        metadata,
        Column("id", String(64), primary_key=True),
        Column("knowledge_base_id", String(64), nullable=False),
    )
    items = Table(
        "knowledge_source_items",
        metadata,
        Column("id", String(64), primary_key=True),
        Column("knowledge_base_id", String(64), nullable=False),
        Column("source_connection_id", String(64), nullable=False),
    )
    syncs = Table(
        "knowledge_sync_runs",
        metadata,
        Column("id", String(64), primary_key=True),
        Column("source_connection_id", String(64), nullable=False),
    )
    jobs = Table(
        "knowledge_import_jobs",
        metadata,
        Column("id", String(64), primary_key=True),
        Column("knowledge_base_id", String(64), nullable=False),
        Column("status", String(40), nullable=False),
        Column("file_name", String(500), nullable=False),
        Column("file_type", String(40), nullable=False),
        Column("file_size", Integer, nullable=False),
        Column("source_path", Text, nullable=False),
        Column("source_sha256", String(64), nullable=False),
        Column("title", String(300)),
        Column("publish_targets", JSON, nullable=False),
        Column("current_step", String(80), nullable=False),
        Column("progress", Integer, nullable=False),
        Column("document_id", String(64)),
        Column("source_connection_id", String(64)),
        Column("source_item_id", String(64)),
        Column("sync_run_id", String(64)),
        Column("error_message", Text),
        Column("retry_count", Integer, nullable=False),
        Column("job_metadata", JSON, nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False),
        Column("updated_at", DateTime(timezone=True), nullable=False),
        Column("started_at", DateTime(timezone=True)),
        Column("finished_at", DateTime(timezone=True)),
        Column("lease_owner", String(120)),
        Column("lease_expires_at", DateTime(timezone=True)),
        Column("heartbeat_at", DateTime(timezone=True)),
        Column("attempt", Integer, nullable=False),
    )
    events = Table(
        "knowledge_import_events",
        metadata,
        Column("id", String(64), primary_key=True),
        Column("job_id", String(64), nullable=False),
        Column("level", String(20), nullable=False),
        Column("message", Text, nullable=False),
        Column("event_metadata", JSON, nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False),
    )
    metadata.create_all(source_engine)
    source_file = tmp_path / "import.md"
    source_file.write_text("import", encoding="utf-8")
    moment = datetime(2026, 9, 3, 0, 5, tzinfo=timezone.utc)
    with source_engine.begin() as connection:
        connection.execute(bases.insert(), {"id": "kb_1"})
        connection.execute(documents.insert(), {"id": "doc_1", "knowledge_base_id": "kb_1"})
        connection.execute(connections.insert(), {"id": "conn_1", "knowledge_base_id": "kb_1"})
        connection.execute(
            items.insert(), {"id": "item_1", "knowledge_base_id": "kb_1", "source_connection_id": "conn_1"}
        )
        connection.execute(syncs.insert(), {"id": "sync_1", "source_connection_id": "conn_1"})
        connection.execute(
            jobs.insert(),
            {
                "id": "job_1",
                "knowledge_base_id": "kb_1",
                "status": "running",
                "file_name": "import.md",
                "file_type": "markdown",
                "file_size": 6,
                "source_path": str(source_file),
                "source_sha256": "abc123",
                "title": "Import",
                "publish_targets": ["rag"],
                "current_step": "parse",
                "progress": 30,
                "document_id": "doc_1",
                "source_connection_id": "conn_1",
                "source_item_id": "item_1",
                "sync_run_id": "sync_1",
                "error_message": "",
                "retry_count": 0,
                "job_metadata": {"kind": "markdown_import", "api_key": "raw"},
                "created_at": moment,
                "updated_at": moment,
                "started_at": moment,
                "finished_at": None,
                "lease_owner": "worker-1",
                "lease_expires_at": moment + timedelta(minutes=5),
                "heartbeat_at": moment + timedelta(minutes=1),
                "attempt": 1,
            },
        )
        connection.execute(
            events.insert(),
            {
                "id": "event_1",
                "job_id": "job_1",
                "level": "info",
                "message": "Started https://example.test?token=raw",
                "event_metadata": {"secret": "raw"},
                "created_at": moment,
            },
        )
    return source_engine, target_engine, jobs, source_file


def test_processing_job_catalog_rehearsal_copies_fenced_job_without_host_path(tmp_path: Path) -> None:
    from jsonschema import Draft202012Validator

    from knowledge_platform.catalog import (
        KnowledgeProcessingJob,
        run_processing_job_catalog_rehearsal_with_rollback_probes,
    )

    source_engine, target_engine, _, source_file = _fixture(tmp_path)
    with target_engine.connect() as connection:
        from knowledge_platform.catalog import migrate_to_latest

        with connection.begin():
            migrate_to_latest(connection)
    with source_engine.connect() as source:
        result = run_processing_job_catalog_rehearsal_with_rollback_probes(
            source,
            target_engine,
            installation_id="install-processing",
            source_revision="legacy-processing-1",
            target_revision="platform-processing-1",
            active_revision="legacy-active-1",
            file_reference_checker=lambda reference: Path(reference).exists(),
            lease_as_of=datetime(2026, 9, 3, 0, 7, tzinfo=timezone.utc),
        )
    assert all(result.report.checks.values())
    assert result.report.retry_idempotent is True
    with target_engine.connect() as connection:
        row = connection.execute(select(KnowledgeProcessingJob)).mappings().one()
        serialized = json.dumps(dict(row), ensure_ascii=False, default=str)
        assert str(source_file) not in serialized and '"api_key": "raw"' not in serialized
        assert row["input_uri"].startswith("knowledge://")
        assert row["input_reference_digest"].startswith("sha256:")
    report_json = json.loads(json.dumps(result.to_dict(), ensure_ascii=False, default=str))
    schema = json.loads(
        (
            Path(__file__).resolve().parents[2] / "docs/knowledge-platform/catalog-rehearsal-report.schema.json"
        ).read_text()
    )
    assert list(Draft202012Validator(schema).iter_errors(report_json)) == []
    source_engine.dispose()
    target_engine.dispose()


def test_processing_job_catalog_rehearsal_rejects_invalid_lease(tmp_path: Path) -> None:
    from knowledge_platform.catalog import (
        RehearsalVerificationError,
        run_processing_job_catalog_rehearsal_with_rollback_probes,
    )

    source_engine, target_engine, jobs, source_file = _fixture(tmp_path)
    with source_engine.begin() as connection:
        connection.execute(jobs.update().values(heartbeat_at=datetime(2026, 9, 3, 0, 11)))
    with source_engine.connect() as source:
        with pytest.raises(RehearsalVerificationError, match="checks failed"):
            run_processing_job_catalog_rehearsal_with_rollback_probes(
                source,
                target_engine,
                installation_id="install-processing",
                source_revision="legacy-1",
                target_revision="platform-1",
                active_revision="legacy-active",
                file_reference_checker=lambda reference: Path(reference).exists(),
                lease_as_of=datetime(2026, 9, 3, 0, 7, tzinfo=timezone.utc),
            )
    source_engine.dispose()
    target_engine.dispose()
