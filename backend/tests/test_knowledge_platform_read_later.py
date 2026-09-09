from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import JSON, Column, DateTime, Integer, MetaData, String, Table, Text, create_engine, select


def _source_tables(metadata: MetaData) -> dict[str, Table]:
    return {
        "bases": Table(
            "knowledge_bases",
            metadata,
            Column("id", String(64), primary_key=True),
            Column("name", String(200), nullable=False),
            Column("description", Text, nullable=False),
            Column("created_at", DateTime(timezone=True), nullable=False),
            Column("updated_at", DateTime(timezone=True), nullable=False),
        ),
        "documents": Table(
            "knowledge_documents",
            metadata,
            Column("id", String(64), primary_key=True),
            Column("knowledge_base_id", String(64), nullable=False),
        ),
        "connections": Table(
            "knowledge_source_connections",
            metadata,
            Column("id", String(64), primary_key=True),
            Column("knowledge_base_id", String(64), nullable=False),
        ),
        "items": Table(
            "knowledge_source_items",
            metadata,
            Column("id", String(64), primary_key=True),
            Column("knowledge_base_id", String(64), nullable=False),
            Column("source_connection_id", String(64), nullable=False),
        ),
        "runs": Table(
            "knowledge_sync_runs",
            metadata,
            Column("id", String(64), primary_key=True),
            Column("source_connection_id", String(64), nullable=False),
        ),
        "captures": Table(
            "read_later_items",
            metadata,
            Column("id", String(64), primary_key=True),
            Column("knowledge_base_id", String(64), nullable=False),
            Column("original_url", Text, nullable=False),
            Column("canonical_url", Text, nullable=False),
            Column("title", String(500), nullable=False),
            Column("site_name", String(300), nullable=False),
            Column("author", String(300), nullable=False),
            Column("description", Text, nullable=False),
            Column("image_url", Text, nullable=False),
            Column("storage_path", Text, nullable=False),
            Column("virtual_path", Text, nullable=False),
            Column("content_sha256", String(64), nullable=False),
            Column("parse_status", String(40), nullable=False),
            Column("reading_status", String(40), nullable=False),
            Column("error_message", Text, nullable=False),
            Column("tags", JSON, nullable=False),
            Column("note", Text, nullable=False),
            Column("document_id", String(64)),
            Column("source_connection_id", String(64)),
            Column("source_item_id", String(64)),
            Column("raw_snapshot_path", Text, nullable=False),
            Column("wiki_job_id", String(64), nullable=False),
            Column("fetched_at", DateTime(timezone=True)),
            Column("read_at", DateTime(timezone=True)),
            Column("created_at", DateTime(timezone=True), nullable=False),
            Column("updated_at", DateTime(timezone=True), nullable=False),
        ),
        "jobs": Table(
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
        ),
        "events": Table(
            "knowledge_import_events",
            metadata,
            Column("id", String(64), primary_key=True),
            Column("job_id", String(64), nullable=False),
            Column("level", String(20), nullable=False),
            Column("message", Text, nullable=False),
            Column("event_metadata", JSON, nullable=False),
            Column("created_at", DateTime(timezone=True), nullable=False),
        ),
    }


def _fixture(tmp_path: Path):
    source_engine = create_engine("sqlite:///:memory:")
    target_engine = create_engine("sqlite:///:memory:")
    metadata = MetaData()
    tables = _source_tables(metadata)
    metadata.create_all(source_engine)
    moment = datetime(2026, 9, 3, 0, 5, tzinfo=timezone.utc)
    source_file = tmp_path / "captured.md"
    snapshot_file = tmp_path / "snapshot.html"
    import_file = tmp_path / "import.md"
    for path in (source_file, snapshot_file, import_file):
        path.write_text("fixture", encoding="utf-8")
    with source_engine.begin() as connection:
        connection.execute(
            tables["bases"].insert(),
            {"id": "kb_1", "name": "Read Later", "description": "", "created_at": moment, "updated_at": moment},
        )
        connection.execute(tables["documents"].insert(), {"id": "doc_1", "knowledge_base_id": "kb_1"})
        connection.execute(tables["connections"].insert(), {"id": "conn_web", "knowledge_base_id": "kb_1"})
        connection.execute(
            tables["items"].insert(),
            {"id": "item_web", "knowledge_base_id": "kb_1", "source_connection_id": "conn_web"},
        )
        connection.execute(tables["runs"].insert(), {"id": "run_web", "source_connection_id": "conn_web"})
        connection.execute(
            tables["captures"].insert(),
            {
                "id": "later_1",
                "knowledge_base_id": "kb_1",
                "original_url": "https://user:password@example.test/articles/1?token=raw&utm_source=mail",
                "canonical_url": "https://example.test/articles/1?token=raw",
                "title": "A captured article",
                "site_name": "Example",
                "author": "Author",
                "description": "Summary",
                "image_url": "https://cdn.example.test/image.png?sig=raw",
                "storage_path": str(source_file),
                "virtual_path": "/knowledge/read-later/a-captured-article.md",
                "content_sha256": "abc123",
                "parse_status": "ready",
                "reading_status": "unread",
                "error_message": "",
                "tags": ["ai", "reading"],
                "note": "Keep this https://note.example.test/private?token=raw",
                "document_id": "doc_1",
                "source_connection_id": "conn_web",
                "source_item_id": "item_web",
                "raw_snapshot_path": str(snapshot_file),
                "wiki_job_id": "",
                "fetched_at": moment,
                "read_at": None,
                "created_at": moment,
                "updated_at": moment,
            },
        )
        connection.execute(
            tables["jobs"].insert(),
            {
                "id": "job_1",
                "knowledge_base_id": "kb_1",
                "status": "running",
                "file_name": "article.md",
                "file_type": "url",
                "file_size": 7,
                "source_path": str(import_file),
                "source_sha256": "def456",
                "title": "A captured article",
                "publish_targets": ["read_later"],
                "current_step": "parse",
                "progress": 40,
                "document_id": "doc_1",
                "source_connection_id": "conn_web",
                "source_item_id": "item_web",
                "sync_run_id": "run_web",
                "error_message": "",
                "retry_count": 0,
                "job_metadata": {"kind": "read_later_capture", "read_later_item_id": "later_1", "api_key": "raw"},
                "created_at": moment,
                "updated_at": moment,
                "started_at": moment,
                "finished_at": None,
                "lease_owner": "worker-capture",
                "lease_expires_at": datetime(2026, 9, 3, 0, 10),
                "heartbeat_at": datetime(2026, 9, 3, 0, 6),
                "attempt": 1,
            },
        )
        connection.execute(
            tables["events"].insert(),
            {
                "id": "event_1",
                "job_id": "job_1",
                "level": "info",
                "message": "Fetched https://example.test/articles/1?token=raw",
                "event_metadata": {"secret": "raw"},
                "created_at": moment,
            },
        )
    return source_engine, target_engine, tables


def test_read_later_catalog_rehearsal_is_independent_redacted_and_rollback_safe(tmp_path: Path) -> None:
    from jsonschema import Draft202012Validator

    from knowledge_platform.catalog import (
        KnowledgeWebCapture,
        run_read_later_catalog_rehearsal_with_rollback_probes,
    )

    source_engine, target_engine, tables = _fixture(tmp_path)
    with target_engine.begin() as connection:
        from knowledge_platform.catalog import migrate_to_latest

        migrate_to_latest(connection)
        connection.execute(
            KnowledgeWebCapture.__table__.insert().values(
                id="web_capture_other",
                space_id="space_other",
                original_url_digest="sha256:other",
                canonical_url_digest="sha256:other",
                title="Other",
                site_name="",
                author="",
                description="",
                image_url_digest="",
                content_uri="knowledge://spaces/space_other/captures/web_capture_other/content",
                raw_snapshot_uri="",
                content_digest="",
                parse_status="queued",
                reading_status="unread",
                error_message="",
                tags_json=[],
                note="",
                asset_id=None,
                connector_id=None,
                source_item_id=None,
                ingestion_job_id=None,
                fetched_at=None,
                read_at=None,
            )
        )
    with source_engine.connect() as source:
        result = run_read_later_catalog_rehearsal_with_rollback_probes(
            source,
            target_engine,
            installation_id="install-read-later",
            source_revision="legacy-read-later-1",
            target_revision="platform-read-later-1",
            active_revision="legacy-active-1",
            file_reference_checker=lambda reference: Path(reference).exists(),
            lease_as_of=datetime(2026, 9, 3, 0, 7, tzinfo=timezone.utc),
        )
    assert all(result.report.checks.values())
    assert result.report.retry_idempotent is True
    assert len(result.report.target_out_of_scope_tables) == 1
    assert [item[0] for item in result.source_to_target] == ["read_later_item"]
    with target_engine.connect() as connection:
        capture = connection.execute(select(KnowledgeWebCapture)).mappings().all()
        serialized = json.dumps([dict(row) for row in capture], ensure_ascii=False, default=str)
        assert "password" not in serialized and "token=raw" not in serialized
        assert str(tmp_path) not in serialized
    report_json = json.loads(json.dumps(result.to_dict(), ensure_ascii=False, default=str))
    schema = json.loads(
        (
            Path(__file__).resolve().parents[2] / "docs/knowledge-platform/catalog-rehearsal-report.schema.json"
        ).read_text()
    )
    assert list(Draft202012Validator(schema).iter_errors(report_json)) == []
    source_engine.dispose()
    target_engine.dispose()


def test_read_later_catalog_rehearsal_rejects_dangling_capture_job(tmp_path: Path) -> None:
    from knowledge_platform.catalog import (
        RehearsalVerificationError,
        run_read_later_catalog_rehearsal_with_rollback_probes,
    )

    source_engine, target_engine, tables = _fixture(tmp_path)
    with source_engine.begin() as connection:
        connection.execute(
            tables["jobs"].update().values(job_metadata={"kind": "read_later_capture", "read_later_item_id": "missing"})
        )
    with source_engine.connect() as source:
        with pytest.raises(RehearsalVerificationError, match="missing read-later capture"):
            run_read_later_catalog_rehearsal_with_rollback_probes(
                source,
                target_engine,
                installation_id="install-read-later",
                source_revision="legacy-1",
                target_revision="platform-1",
                active_revision="legacy-active",
                file_reference_checker=lambda reference: Path(reference).exists(),
                lease_as_of=datetime(2026, 9, 3, 0, 7, tzinfo=timezone.utc),
            )
    source_engine.dispose()
    target_engine.dispose()


def test_read_later_catalog_rehearsal_rejects_untrusted_revision_before_copy(tmp_path: Path) -> None:
    from knowledge_platform.catalog import (
        RehearsalVerificationError,
        run_read_later_catalog_rehearsal_with_rollback_probes,
    )

    source_engine, target_engine, _ = _fixture(tmp_path)
    with source_engine.connect() as source:
        with pytest.raises(RehearsalVerificationError, match="unsafe read-later source revision"):
            run_read_later_catalog_rehearsal_with_rollback_probes(
                source,
                target_engine,
                installation_id="install-read-later",
                source_revision="../secret",
                target_revision="platform-1",
                active_revision="legacy-active",
                file_reference_checker=lambda reference: Path(reference).exists(),
                lease_as_of=datetime(2026, 9, 3, 0, 7, tzinfo=timezone.utc),
            )
        with pytest.raises(RehearsalVerificationError, match="unsafe read-later target revision"):
            run_read_later_catalog_rehearsal_with_rollback_probes(
                source,
                target_engine,
                installation_id="install-read-later",
                source_revision="legacy-1",
                target_revision="../token",
                active_revision="legacy-active",
                file_reference_checker=lambda reference: Path(reference).exists(),
                lease_as_of=datetime(2026, 9, 3, 0, 7, tzinfo=timezone.utc),
            )
    source_engine.dispose()
    target_engine.dispose()
