from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import JSON, Column, DateTime, MetaData, String, Table, Text, create_engine, select


def _fixture():
    source_engine = create_engine("sqlite:///:memory:")
    target_engine = create_engine("sqlite:///:memory:")
    metadata = MetaData()
    notifications = Table(
        "task_notifications",
        metadata,
        Column("id", String(64), primary_key=True),
        Column("category", String(80), nullable=False),
        Column("subject_type", String(80), nullable=False),
        Column("subject_id", String(64), nullable=False),
        Column("title", String(300), nullable=False),
        Column("body", Text, nullable=False),
        Column("payload", JSON, nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False),
        Column("read_at", DateTime(timezone=True)),
    )
    metadata.create_all(source_engine)
    moment = datetime(2026, 9, 3, 1, 5, tzinfo=timezone.utc)
    with source_engine.begin() as connection:
        connection.execute(
            notifications.insert(),
            {
                "id": "ntf_1",
                "category": "semantic_dimension_build",
                "subject_type": "semantic_dimension_build_job",
                "subject_id": "job_1",
                "title": "Sales dimension published",
                "body": "See https://example.test/result?token=raw",
                "payload": {
                    "job_id": "job_1",
                    "dimension_id": "sales",
                    "status": "published",
                    "session_id": "session-secret",
                    "query_id": "query-secret",
                },
                "created_at": moment,
                "read_at": moment,
            },
        )
    return source_engine, target_engine, notifications


def test_notification_event_rehearsal_splits_harness_read_state_and_redacts_correlation() -> None:
    from knowledge_platform.catalog import (
        KnowledgeNotificationEvent,
        run_notification_event_catalog_rehearsal_with_rollback_probes,
    )
    from knowledge_platform.catalog.notification_event_rehearsal import _source_ref_digest

    source_engine, target_engine, _ = _fixture()
    with source_engine.connect() as source:
        result = run_notification_event_catalog_rehearsal_with_rollback_probes(
            source,
            target_engine,
            installation_id="install-notifications",
            source_revision="legacy-notifications-1",
            target_revision="platform-notifications-1",
            active_revision="legacy-active-1",
        )
    assert all(result.report.checks.values())
    assert result.report.retry_idempotent is True
    with target_engine.connect() as connection:
        row = connection.execute(select(KnowledgeNotificationEvent)).mappings().one()
    assert "read_at" not in row
    assert row["payload_json"] == {
        "job_id": "job_1",
        "dimension_id": "sales",
        "status": "published",
        "session_id_digest": _source_ref_digest("session-secret"),
        "query_id_digest": _source_ref_digest("query-secret"),
    }
    serialized = json.dumps(dict(row), ensure_ascii=False, default=str)
    assert "session-secret" not in serialized
    assert "query-secret" not in serialized
    assert "https://example.test" not in serialized
    source_engine.dispose()
    target_engine.dispose()


def test_notification_event_rehearsal_rejects_unknown_payload_fields() -> None:
    from knowledge_platform.catalog import (
        RehearsalVerificationError,
        run_notification_event_catalog_rehearsal_with_rollback_probes,
    )

    source_engine, target_engine, notifications = _fixture()
    with source_engine.begin() as connection:
        connection.execute(notifications.update().values(payload={"secret_blob": "raw"}))
    with source_engine.connect() as source:
        with pytest.raises(RehearsalVerificationError, match="unsupported payload keys"):
            run_notification_event_catalog_rehearsal_with_rollback_probes(
                source,
                target_engine,
                installation_id="install-notifications",
                source_revision="legacy-notifications-1",
                target_revision="platform-notifications-1",
                active_revision="legacy-active-1",
                failure_checkpoints=("after_schema",),
            )
    source_engine.dispose()
    target_engine.dispose()


def test_notification_event_rehearsal_rejects_same_sqlite_file_alias(tmp_path) -> None:
    from knowledge_platform.catalog import (
        RehearsalVerificationError,
        run_notification_event_catalog_rehearsal_with_rollback_probes,
    )

    path = tmp_path / "shared.sqlite"
    source_engine = create_engine(f"sqlite:///{path}")
    target_engine = create_engine(f"sqlite:///{path.resolve()}")
    metadata = MetaData()
    Table(
        "task_notifications",
        metadata,
        Column("id", String(64), primary_key=True),
        Column("category", String(80), nullable=False),
        Column("subject_type", String(80), nullable=False),
        Column("subject_id", String(64), nullable=False),
        Column("title", String(300), nullable=False),
        Column("body", Text, nullable=False),
        Column("payload", JSON, nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False),
        Column("read_at", DateTime(timezone=True)),
    )
    metadata.create_all(source_engine)
    with source_engine.connect() as source:
        with pytest.raises(RehearsalVerificationError, match="independent engines"):
            run_notification_event_catalog_rehearsal_with_rollback_probes(
                source,
                target_engine,
                installation_id="install-notifications",
                source_revision="legacy-notifications-1",
                target_revision="platform-notifications-1",
                active_revision="legacy-active-1",
                failure_checkpoints=("after_schema",),
            )
    source_engine.dispose()
    target_engine.dispose()


def test_notification_contract_and_orm_reject_unsafe_values() -> None:
    from sqlalchemy.orm import Session

    from knowledge_contracts import NotificationEvent
    from knowledge_platform.catalog import KnowledgeNotificationEvent, migrate_to_latest

    with pytest.raises(ValueError, match="non-portable"):
        NotificationEvent(
            event_id="notification_1",
            event_type="task_notification.v1",
            subject_type="job",
            subject_id="job_1",
            title="Open file:/etc/passwd",
            occurred_at="2026-09-03T01:05:00Z",
        )
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        migrate_to_latest(connection)
    with Session(engine) as session:
        session.add(
            KnowledgeNotificationEvent(
                id="notification_1",
                event_type="task_notification.v1",
                category="semantic",
                subject_type="job",
                subject_id="job_1",
                title="Open file:/etc/passwd",
                body="",
                payload_json={},
                created_at=datetime(2026, 9, 3, tzinfo=timezone.utc),
            )
        )
        with pytest.raises(ValueError, match="non-portable"):
            session.flush()
    engine.dispose()


def test_platform_notification_reads_require_explicit_space_binding(tmp_path) -> None:
    from knowledge_contracts import Correlation, NotificationEvent, Principal
    from knowledge_platform.catalog import migrate_to_latest
    from knowledge_platform.catalog.notification_scope import SqliteNotificationEventScopeStore
    from knowledge_platform.catalog.notification_service import NotificationEventQueryService

    database = tmp_path / "platform-notifications.sqlite3"
    engine = create_engine(f"sqlite:///{database}")
    with engine.begin() as connection:
        migrate_to_latest(connection)
    engine.dispose()

    store = SqliteNotificationEventScopeStore(database)
    event = NotificationEvent(
        event_id="notification_scoped_1",
        event_type="task_notification.v1",
        subject_type="job",
        subject_id="job_1",
        title="Job finished",
        occurred_at="2026-09-03T01:05:00Z",
        payload={"status": "published"},
    )
    store.publish(event=event, category="processing", space_id="space_1")
    assert store.list_events(space_id="space_2") == []
    assert store.list_events(space_id="space_1")[0]["space_id"] == "space_1"
    with pytest.raises(ValueError, match="another Space"):
        store.bind_event(event_id=event.event_id, space_id="space_2")

    service = NotificationEventQueryService(store)
    result = service.list_events(
        principal=Principal(
            subject_id="admin",
            scopes=("knowledge.admin", "knowledge.space:space_1"),
        ),
        correlation=Correlation("notification-scope-test"),
        space_id="space_1",
        limit=10,
    )
    assert result.status == "ok"
    assert result.data["count"] == 1
    assert result.data["notifications"][0]["resource_uri"] == "knowledge://events/notifications/notification_scoped_1"
    denied = service.list_events(
        principal=Principal(subject_id="tenant-user", tenant_id="tenant-1"),
        correlation=Correlation("notification-scope-denied"),
        space_id="space_1",
    )
    assert denied.error.code.value == "permission_denied"
