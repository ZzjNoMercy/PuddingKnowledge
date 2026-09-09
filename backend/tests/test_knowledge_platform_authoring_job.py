from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import JSON, Column, DateTime, Integer, MetaData, String, Table, Text, create_engine, inspect, select


def _fixture(tmp_path: Path):
    source_engine = create_engine("sqlite:///:memory:")
    target_engine = create_engine("sqlite:///:memory:")
    metadata = MetaData()
    jobs = Table(
        "semantic_dimension_build_jobs",
        metadata,
        Column("id", String(64), primary_key=True),
        Column("session_id", String(64), nullable=False),
        Column("query_id", String(64), nullable=False),
        Column("dimension_id", String(120), nullable=False),
        Column("adapter", String(120), nullable=False),
        Column("requested_scope", JSON, nullable=False),
        Column("input_snapshot", JSON, nullable=False),
        Column("status", String(80), nullable=False),
        Column("current_step", String(80), nullable=False),
        Column("progress", Integer, nullable=False),
        Column("staging_path", Text),
        Column("published_reference_path", Text),
        Column("result_summary", JSON, nullable=False),
        Column("error_message", Text),
        Column("retry_count", Integer, nullable=False),
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
        "semantic_dimension_build_events",
        metadata,
        Column("id", String(64), primary_key=True),
        Column("job_id", String(64), nullable=False),
        Column("level", String(20), nullable=False),
        Column("message", Text, nullable=False),
        Column("event_metadata", JSON, nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False),
    )
    metadata.create_all(source_engine)
    staging_file = tmp_path / "semantic-staging.json"
    published_file = tmp_path / "semantic-published.json"
    staging_file.write_text("staged", encoding="utf-8")
    published_file.write_text("published", encoding="utf-8")
    moment = datetime(2026, 9, 3, 0, 5, tzinfo=timezone.utc)
    with source_engine.begin() as connection:
        connection.execute(
            jobs.insert(),
            {
                "id": "build_1",
                "session_id": "session-secret",
                "query_id": "query-secret",
                "dimension_id": "dim_sales",
                "adapter": "sql_adapter",
                "requested_scope": {
                    "tenant": "tenant_1",
                    "source_path": str(staging_file),
                    "callback": "https://example.test/callback?token=raw",
                },
                "input_snapshot": {"api_key": "raw-secret", "query": "select * from sales"},
                "status": "waiting_for_publish_confirmation",
                "current_step": "waiting_for_publish_confirmation",
                "progress": 100,
                "staging_path": str(staging_file),
                "published_reference_path": str(published_file),
                "result_summary": {"artifact_path": str(published_file), "secret": "raw-secret"},
                "error_message": "",
                "retry_count": 1,
                "created_at": moment,
                "updated_at": moment,
                "started_at": moment,
                "finished_at": moment,
                "lease_owner": None,
                "lease_expires_at": None,
                "heartbeat_at": None,
                "attempt": 1,
            },
        )
        connection.execute(
            events.insert(),
            {
                "id": "event_1",
                "job_id": "build_1",
                "level": "info",
                "message": "Prepared https://example.test?token=raw",
                "event_metadata": {"secret": "raw-secret"},
                "created_at": moment,
            },
        )
    return source_engine, target_engine, jobs, staging_file, published_file


def test_authoring_job_catalog_rehearsal_redacts_correlations_and_rolls_back(tmp_path: Path) -> None:
    from jsonschema import Draft202012Validator

    from knowledge_platform.catalog import (
        KnowledgeAuthoringJob,
        migrate_to_latest,
        run_authoring_job_catalog_rehearsal_with_rollback_probes,
    )

    source_engine, target_engine, _, staging_file, published_file = _fixture(tmp_path)
    moment = datetime(2026, 9, 3, 0, 5, tzinfo=timezone.utc)
    with target_engine.begin() as connection:
        migrate_to_latest(connection)
        connection.execute(
            KnowledgeAuthoringJob.__table__.insert().values(
                id="authoring_other",
                kind="semantic_dimension_build",
                dimension_id="dim_other",
                adapter="sql_adapter",
                scope_uri="knowledge://semantic-dimensions/dim_other",
                scope_json={},
                input_snapshot_json={},
                status="published",
                current_step="published",
                progress=100,
                staging_uri="",
                staging_reference_digest="",
                published_uri="",
                published_reference_digest="",
                result_summary_json={},
                correlation_json={},
                error_message="",
                retry_count=0,
                metadata_json={},
                created_at=moment,
                updated_at=moment,
                started_at=moment,
                finished_at=moment,
                lease_owner=None,
                lease_expires_at=None,
                heartbeat_at=None,
                attempt=1,
            )
        )
    with source_engine.connect() as source:
        result = run_authoring_job_catalog_rehearsal_with_rollback_probes(
            source,
            target_engine,
            installation_id="install-authoring",
            source_revision="legacy-authoring-1",
            target_revision="platform-authoring-1",
            active_revision="legacy-active-1",
            file_reference_checker=lambda reference: Path(reference).exists(),
        )
    assert all(result.report.checks.values())
    assert result.report.retry_idempotent is True
    assert len(result.report.target_out_of_scope_tables) == 1
    with target_engine.connect() as connection:
        row = (
            connection.execute(select(KnowledgeAuthoringJob).where(KnowledgeAuthoringJob.id == "authoring_build_1"))
            .mappings()
            .one()
        )
        serialized = json.dumps(dict(row), ensure_ascii=False, default=str)
        assert str(staging_file) not in serialized and str(published_file) not in serialized
        assert "session-secret" not in serialized and "query-secret" not in serialized
        assert "raw-secret" not in serialized and "token=raw" not in serialized
        assert row["staging_uri"].startswith("knowledge://")
        assert row["published_uri"].startswith("knowledge://")
        assert row["staging_reference_digest"].startswith("sha256:")
        foreign_keys = inspect(connection).get_foreign_keys("knowledge_authoring_events")
        assert foreign_keys[0]["referred_table"] == "knowledge_authoring_jobs"
    report_json = json.loads(json.dumps(result.to_dict(), ensure_ascii=False, default=str))
    schema = json.loads(
        (
            Path(__file__).resolve().parents[2] / "docs/knowledge-platform/catalog-rehearsal-report.schema.json"
        ).read_text()
    )
    assert list(Draft202012Validator(schema).iter_errors(report_json)) == []
    source_engine.dispose()
    target_engine.dispose()


def test_authoring_job_catalog_rehearsal_rejects_lease_on_waiting_job(tmp_path: Path) -> None:
    from knowledge_platform.catalog import (
        RehearsalVerificationError,
        run_authoring_job_catalog_rehearsal_with_rollback_probes,
    )

    source_engine, target_engine, jobs, _, _ = _fixture(tmp_path)
    with source_engine.begin() as connection:
        connection.execute(jobs.update().values(lease_owner="worker-1"))
    with source_engine.connect() as source:
        with pytest.raises(RehearsalVerificationError, match="checks failed"):
            run_authoring_job_catalog_rehearsal_with_rollback_probes(
                source,
                target_engine,
                installation_id="install-authoring",
                source_revision="legacy-1",
                target_revision="platform-1",
                active_revision="legacy-active",
                file_reference_checker=lambda reference: Path(reference).exists(),
            )
    source_engine.dispose()
    target_engine.dispose()


def test_authoring_job_catalog_rehearsal_rejects_unknown_event_level(tmp_path: Path) -> None:
    from knowledge_platform.catalog import (
        RehearsalVerificationError,
        run_authoring_job_catalog_rehearsal_with_rollback_probes,
    )

    source_engine, target_engine, _, _, _ = _fixture(tmp_path)
    events = Table("semantic_dimension_build_events", MetaData(), autoload_with=source_engine)
    with source_engine.begin() as connection:
        connection.execute(events.update().values(level="token=LEAK"))
    with source_engine.connect() as source:
        with pytest.raises(RehearsalVerificationError, match="unknown event level"):
            run_authoring_job_catalog_rehearsal_with_rollback_probes(
                source,
                target_engine,
                installation_id="install-authoring",
                source_revision="legacy-1",
                target_revision="platform-1",
                active_revision="legacy-active",
                file_reference_checker=lambda reference: Path(reference).exists(),
            )
    source_engine.dispose()
    target_engine.dispose()


def test_authoring_job_catalog_rehearsal_requires_checker_for_relative_and_file_paths(tmp_path: Path) -> None:
    from knowledge_platform.catalog import (
        RehearsalVerificationError,
        run_authoring_job_catalog_rehearsal_with_rollback_probes,
    )

    source_engine, target_engine, jobs, _, _ = _fixture(tmp_path)
    with source_engine.begin() as connection:
        connection.execute(
            jobs.update().values(
                staging_path="../missing/staging.json",
                published_reference_path="file:///missing/published.json",
            )
        )
    with source_engine.connect() as source:
        with pytest.raises(RehearsalVerificationError, match="file_reference_checker is required"):
            run_authoring_job_catalog_rehearsal_with_rollback_probes(
                source,
                target_engine,
                installation_id="install-authoring",
                source_revision="legacy-1",
                target_revision="platform-1",
                active_revision="legacy-active",
            )
    source_engine.dispose()
    target_engine.dispose()


def test_authoring_job_catalog_rehearsal_rejects_derived_event_id_overflow(tmp_path: Path) -> None:
    from knowledge_platform.catalog import (
        RehearsalVerificationError,
        run_authoring_job_catalog_rehearsal_with_rollback_probes,
    )

    source_engine, target_engine, _, _, _ = _fixture(tmp_path)
    events = Table("semantic_dimension_build_events", MetaData(), autoload_with=source_engine)
    with source_engine.begin() as connection:
        connection.execute(events.update().values(id="e" * 145))
    with source_engine.connect() as source:
        with pytest.raises(RehearsalVerificationError, match="unsafe authoring-job event id"):
            run_authoring_job_catalog_rehearsal_with_rollback_probes(
                source,
                target_engine,
                installation_id="install-authoring",
                source_revision="legacy-1",
                target_revision="platform-1",
                active_revision="legacy-active",
                file_reference_checker=lambda reference: Path(reference).exists(),
            )
    source_engine.dispose()
    target_engine.dispose()


def test_authoring_job_catalog_rehearsal_accepts_fenced_running_job(tmp_path: Path) -> None:
    from knowledge_platform.catalog import run_authoring_job_catalog_rehearsal_with_rollback_probes

    source_engine, target_engine, jobs, _, _ = _fixture(tmp_path)
    moment = datetime(2026, 9, 3, 0, 5, tzinfo=timezone.utc)
    with source_engine.begin() as connection:
        connection.execute(
            jobs.update().values(
                status="running",
                current_step="load_source_profiles",
                progress=5,
                lease_owner="authoring-worker-1",
                lease_expires_at=moment + timedelta(minutes=5),
                heartbeat_at=moment + timedelta(minutes=1),
                finished_at=None,
            )
        )
    with source_engine.connect() as source:
        result = run_authoring_job_catalog_rehearsal_with_rollback_probes(
            source,
            target_engine,
            installation_id="install-authoring",
            source_revision="legacy-running-1",
            target_revision="platform-running-1",
            active_revision="legacy-active-1",
            file_reference_checker=lambda reference: Path(reference).exists(),
            lease_as_of=datetime(2026, 9, 3, 0, 7, tzinfo=timezone.utc),
        )
    assert result.report.checks["lease_state"] is True
    source_engine.dispose()
    target_engine.dispose()
