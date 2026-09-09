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
    results = Table(
        "analytics_query_results",
        metadata,
        Column("id", String(64), primary_key=True),
        Column("session_id", String(120), nullable=False),
        Column("tool_call_id", String(120), nullable=False),
        Column("question", Text, nullable=False),
        Column("sql", Text, nullable=False),
        Column("columns", JSON, nullable=False),
        Column("row_count", Integer, nullable=False),
        Column("profile_json", JSON, nullable=False),
        Column("artifact_path", Text, nullable=False),
        Column("artifact_format", String(20), nullable=False),
        Column("status", String(40), nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False),
        Column("expires_at", DateTime(timezone=True), nullable=False),
    )
    metadata.create_all(source_engine)
    artifact = tmp_path / "query-result.jsonl"
    artifact.write_text('{"brand":"A"}\n', encoding="utf-8")
    moment = datetime(2026, 9, 3, 0, 5, tzinfo=timezone.utc)
    with source_engine.begin() as connection:
        connection.execute(
            results.insert(),
            {
                "id": "qr_1",
                "session_id": "session-private",
                "tool_call_id": "tool-private",
                "question": "Which brand? https://example.test/private?token=raw",
                "sql": "SELECT * FROM sales WHERE token = 'raw'",
                "columns": ["brand", "sales"],
                "row_count": 1,
                "profile_json": {"session_id": "session-private", "artifact_path": str(artifact), "api_key": "raw"},
                "artifact_path": str(artifact),
                "artifact_format": "jsonl",
                "status": "ready",
                "created_at": moment,
                "expires_at": moment + timedelta(hours=1),
            },
        )
    return source_engine, target_engine, results, artifact


def test_query_result_catalog_rehearsal_is_generic_and_redacted(tmp_path: Path) -> None:
    from jsonschema import Draft202012Validator

    from knowledge_platform.catalog import (
        KnowledgeQueryResult,
        migrate_to_latest,
        run_query_result_catalog_rehearsal_with_rollback_probes,
    )

    source_engine, target_engine, _, artifact = _fixture(tmp_path)
    with target_engine.begin() as connection:
        migrate_to_latest(connection)
        connection.execute(
            KnowledgeQueryResult.__table__.insert().values(
                id="query_result_other",
                status="ready",
                question="other",
                sql_digest="sha256:other",
                columns_json=[],
                row_count=0,
                profile_json={},
                artifact_uri="",
                artifact_reference_digest="",
                artifact_format="jsonl",
                correlation_json={},
                created_at=datetime(2026, 9, 3, tzinfo=timezone.utc),
                expires_at=datetime(2026, 9, 4, tzinfo=timezone.utc),
            )
        )
    with source_engine.connect() as source:
        result = run_query_result_catalog_rehearsal_with_rollback_probes(
            source,
            target_engine,
            installation_id="install-query",
            source_revision="legacy-query-1",
            target_revision="platform-query-1",
            active_revision="legacy-active-1",
            file_reference_checker=lambda reference: Path(reference).exists(),
        )
    assert all(result.report.checks.values())
    assert result.report.retry_idempotent is True
    assert len(result.report.target_out_of_scope_tables) == 1
    with target_engine.connect() as connection:
        row = (
            connection.execute(select(KnowledgeQueryResult).where(KnowledgeQueryResult.id == "query_result_qr_1"))
            .mappings()
            .one()
        )
        serialized = json.dumps(dict(row), ensure_ascii=False, default=str)
        assert "session-private" not in serialized and "tool-private" not in serialized
        assert "token = 'raw'" not in serialized and str(artifact) not in serialized
        assert row["artifact_uri"].startswith("knowledge://")
        assert row["sql_digest"].startswith("sha256:")
        assert row["correlation_json"]["session_id_digest"].startswith("sha256:")
    report_json = json.loads(json.dumps(result.to_dict(), ensure_ascii=False, default=str))
    schema = json.loads(
        (
            Path(__file__).resolve().parents[2] / "docs/knowledge-platform/catalog-rehearsal-report.schema.json"
        ).read_text()
    )
    assert list(Draft202012Validator(schema).iter_errors(report_json)) == []
    source_engine.dispose()
    target_engine.dispose()


def test_query_result_catalog_rehearsal_rejects_invalid_status(tmp_path: Path) -> None:
    from knowledge_platform.catalog import (
        RehearsalVerificationError,
        run_query_result_catalog_rehearsal_with_rollback_probes,
    )

    source_engine, target_engine, results, _ = _fixture(tmp_path)
    with source_engine.begin() as connection:
        connection.execute(results.update().values(status="running"))
    with source_engine.connect() as source:
        with pytest.raises(RehearsalVerificationError, match="unknown status"):
            run_query_result_catalog_rehearsal_with_rollback_probes(
                source,
                target_engine,
                installation_id="install-query",
                source_revision="legacy-1",
                target_revision="platform-1",
                active_revision="legacy-active",
                file_reference_checker=lambda reference: Path(reference).exists(),
            )
    source_engine.dispose()
    target_engine.dispose()
