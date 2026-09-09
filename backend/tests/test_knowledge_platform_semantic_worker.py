from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from knowledge_contracts import Principal
from knowledge_platform.catalog import (
    SqliteSemanticDimensionJobStore,
    SqliteSemanticDimensionJobWriter,
    migrate_to_latest,
)
from knowledge_platform.semantic import (
    LocalSemanticDimensionBuilder,
    LocalSemanticDimensionPublisher,
    SemanticDimensionBuildError,
    SemanticDimensionBuildWorker,
)

SOURCE_ID = "structured_source"
SOURCE_DIGEST = "sha256:" + "a" * 64


def _catalog(path: Path) -> None:
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as connection:
        migrate_to_latest(connection)
    engine.dispose()


def _source(path: Path, *, space_id: str = "space_local", digest: str = SOURCE_DIGEST) -> None:
    now = datetime.now(UTC).isoformat()
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            INSERT INTO knowledge_structured_assets (
                id, space_id, source_key, document_asset_id, source_type, file_name, sheet_name,
                size_bytes, modified_at, source_uri, source_reference_digest, logical_path_digest,
                profile_uri, profile_reference_digest, content_digest, profile_status, row_count,
                column_count, columns_json, reference_status, capabilities, metadata_json, created_at, updated_at
            ) VALUES (?, ?, ?, NULL, 'csv', 'source.csv', NULL, 1, NULL, ?, ?, '', ?, ?, ?, 'ready', 1, 1, ?, 'ready', ?, '{}', ?, ?)
            """,
            (
                SOURCE_ID,
                space_id,
                SOURCE_ID,
                f"knowledge://spaces/{space_id}/structured-assets/{SOURCE_ID}/source",
                digest,
                f"knowledge://spaces/{space_id}/structured-assets/{SOURCE_ID}/profile",
                digest,
                digest,
                json.dumps(["value"]),
                json.dumps(["table_query"]),
                now,
                now,
            ),
        )


def _job(path: Path, *, dimension_id: str = "dimension_local", space_id: str = "space_local") -> str:
    principal = Principal("admin", ("knowledge.admin", f"knowledge.space:{space_id}"))
    source_snapshot = ({"asset_id": SOURCE_ID, "content_digest": SOURCE_DIGEST},)
    record = SqliteSemanticDimensionJobWriter(path).create_authoring_job(
        record={
            "id": f"authoring_{dimension_id}",
            "kind": "semantic_dimension_build",
            "dimension_id": dimension_id,
            "adapter": "local",
            "scope_uri": f"knowledge://spaces/{space_id}/semantic-dimensions/{dimension_id}",
            "scope_json": {},
            "input_snapshot_json": {"source_asset_ids": [SOURCE_ID], "source_snapshot": list(source_snapshot)},
            "status": "queued",
            "current_step": "queued",
            "progress": 0,
            "source_snapshot": source_snapshot,
            "principal": principal,
        }
    )
    return str(record["id"])


def test_semantic_dimension_worker_stages_then_can_be_requeued_after_admin_confirm(tmp_path: Path) -> None:
    catalog = tmp_path / "catalog.sqlite3"
    _catalog(catalog)
    _source(catalog)
    job_id = _job(catalog)
    store = SqliteSemanticDimensionJobStore(database_path=catalog)
    worker = SemanticDimensionBuildWorker(
        builder=LocalSemanticDimensionBuilder(),
        jobs=store,
        publisher=LocalSemanticDimensionPublisher(root=tmp_path / "published"),
    )

    first = worker.process(job_id=job_id, space_id="space_local")
    with sqlite3.connect(catalog) as connection:
        row = connection.execute(
            "SELECT status, current_step, progress, staging_uri, staging_reference_digest, published_uri, lease_owner "
            "FROM knowledge_authoring_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
    assert first.staging_digest == row[4]
    assert row[:3] == ("waiting_for_publish_confirmation", "waiting_for_publish_confirmation", 90)
    assert row[3].endswith("/staging") and row[5] == "" and row[6] is None

    actor = "sha256:" + hashlib.sha256(b"admin").hexdigest()
    correlation = "sha256:" + hashlib.sha256(b"decision").hexdigest()
    SqliteSemanticDimensionJobWriter(catalog).decide_authoring_job(
        job_id=job_id,
        space_id="space_local",
        decision="confirm",
        expected_status="waiting_for_publish_confirmation",
        actor_digest=actor,
        correlation_digest=correlation,
    )
    second = worker.process(job_id=job_id, space_id="space_local")
    assert second == first
    with sqlite3.connect(catalog) as connection:
        assert connection.execute("SELECT status, attempt, published_uri FROM knowledge_authoring_jobs WHERE id = ?", (job_id,)).fetchone() == (
            "published",
            2,
            "knowledge://spaces/space_local/semantic-dimensions/dimension_local/published",
        )


def test_semantic_dimension_worker_releases_failed_claim_and_fences_space(tmp_path: Path) -> None:
    catalog = tmp_path / "catalog.sqlite3"
    _catalog(catalog)
    _source(catalog)
    job_id = _job(catalog)
    store = SqliteSemanticDimensionJobStore(database_path=catalog)
    with pytest.raises(PermissionError, match="another Space"):
        store.claim(job_id=job_id, space_id="space_other")

    class FailingBuilder:
        def build(self, build_input: object) -> object:
            del build_input
            raise RuntimeError("builder failed")

    worker = SemanticDimensionBuildWorker(builder=FailingBuilder(), jobs=store)
    with pytest.raises(RuntimeError, match="builder failed"):
        worker.process(job_id=job_id, space_id="space_local")
    with sqlite3.connect(catalog) as connection:
        row = connection.execute(
            "SELECT status, current_step, progress, retry_count, lease_owner FROM knowledge_authoring_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
    assert row == ("queued", "queued", 0, 1, None)


def test_semantic_dimension_worker_does_not_claim_waiting_job_or_stale_source(tmp_path: Path) -> None:
    catalog = tmp_path / "catalog.sqlite3"
    _catalog(catalog)
    _source(catalog)
    job_id = _job(catalog)
    store = SqliteSemanticDimensionJobStore(database_path=catalog)
    worker = SemanticDimensionBuildWorker(builder=LocalSemanticDimensionBuilder(), jobs=store)
    worker.process(job_id=job_id, space_id="space_local")
    with pytest.raises(SemanticDimensionBuildError, match="not claimable"):
        worker.process(job_id=job_id, space_id="space_local")
    SqliteSemanticDimensionJobWriter(catalog).decide_authoring_job(
        job_id=job_id,
        space_id="space_local",
        decision="confirm",
        expected_status="waiting_for_publish_confirmation",
        actor_digest="sha256:" + "b" * 64,
        correlation_digest="sha256:" + "c" * 64,
    )
    with sqlite3.connect(catalog) as connection:
        connection.execute("UPDATE knowledge_structured_assets SET content_digest = ? WHERE id = ?", ("sha256:" + "d" * 64, SOURCE_ID))
    with pytest.raises(ValueError, match="no longer approved"):
        store.claim(job_id=job_id, space_id="space_local")
