from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from knowledge_contracts import Correlation, Principal


def _principal(*scopes: str) -> Principal:
    return Principal(subject_id="admin", scopes=tuple(scopes))


def _create_catalog(path: Path) -> None:
    with sqlite3.connect(path) as connection:
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
            CREATE TABLE knowledge_authoring_jobs (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, dimension_id TEXT NOT NULL, adapter TEXT NOT NULL,
                scope_uri TEXT NOT NULL, scope_json TEXT NOT NULL, input_snapshot_json TEXT NOT NULL,
                status TEXT NOT NULL, current_step TEXT NOT NULL, progress INTEGER NOT NULL,
                staging_uri TEXT NOT NULL, staging_reference_digest TEXT NOT NULL, published_uri TEXT NOT NULL,
                published_reference_digest TEXT NOT NULL, result_summary_json TEXT NOT NULL,
                correlation_json TEXT NOT NULL, error_message TEXT NOT NULL, retry_count INTEGER NOT NULL,
                metadata_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                started_at TEXT, finished_at TEXT, lease_owner TEXT, lease_expires_at TEXT,
                heartbeat_at TEXT, attempt INTEGER NOT NULL
            );
            CREATE TABLE knowledge_authoring_events (
                id TEXT PRIMARY KEY, job_id TEXT NOT NULL, level TEXT NOT NULL,
                message TEXT NOT NULL, metadata_json TEXT NOT NULL, created_at TEXT NOT NULL
            );
            """
        )
        connection.execute(
            """INSERT INTO knowledge_structured_assets VALUES
            (?, ?, ?, NULL, 'excel', 'Jan', NULL, 1, NULL, ?, '', '', '', '', ?, 'ready', 1, 1,
             '["sales"]', 'ready', '["table_query"]', '{}', 'now', 'now')""",
            (
                "tbl_jan",
                "space_sales",
                "tbl_jan",
                "knowledge://spaces/space_sales/structured-assets/tbl_jan/source",
                "sha256:" + "1" * 64,
            ),
        )
        connection.commit()


def test_semantic_dimension_authoring_enqueues_platform_job_idempotently(tmp_path: Path) -> None:
    from knowledge_platform.catalog import SqliteCatalogQueryRepository, SqliteSemanticDimensionJobWriter
    from knowledge_platform.semantic import SemanticDimensionAuthoringRequest, SemanticDimensionAuthoringService

    database = tmp_path / "catalog.sqlite3"
    _create_catalog(database)
    service = SemanticDimensionAuthoringService(
        catalog=SqliteCatalogQueryRepository(database),
        writer=SqliteSemanticDimensionJobWriter(database),
    )
    request = SemanticDimensionAuthoringRequest(
        dimension_id="vehicle_series",
        space_id="space_sales",
        adapter="entity_crosswalk_v1",
        requested_scope={"brands": ["all"]},
        input_snapshot={"source_asset_ids": ["tbl_jan"]},
        title="Vehicle series",
    )
    result = service.enqueue(
        principal=_principal("knowledge:admin", "knowledge:space:space_sales"),
        correlation=Correlation("trace-semantic-authoring"),
        request=request,
    )
    assert result.status == "ok"
    job = result.data["job"]
    assert job["status"] == "queued"
    assert job["lease_owner"] is None
    assert str(job["scope_uri"]).endswith("/semantic-dimensions/vehicle_series")

    replay = service.enqueue(
        principal=_principal("knowledge:admin", "knowledge:space:space_sales"),
        correlation=Correlation("trace-semantic-authoring-replay"),
        request=request,
    )
    assert replay.status == "ok"
    assert replay.data["job"]["id"] == job["id"]
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM knowledge_authoring_jobs").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM knowledge_authoring_events").fetchone()[0] == 1


def test_semantic_dimension_authoring_rejects_tenant_and_unsafe_snapshot(tmp_path: Path) -> None:
    from knowledge_platform.catalog import SqliteCatalogQueryRepository, SqliteSemanticDimensionJobWriter
    from knowledge_platform.semantic import SemanticDimensionAuthoringRequest, SemanticDimensionAuthoringService

    database = tmp_path / "catalog.sqlite3"
    _create_catalog(database)
    service = SemanticDimensionAuthoringService(
        catalog=SqliteCatalogQueryRepository(database),
        writer=SqliteSemanticDimensionJobWriter(database),
    )
    request = SemanticDimensionAuthoringRequest(
        dimension_id="vehicle_series",
        space_id="space_sales",
        adapter="entity_crosswalk_v1",
        input_snapshot={"source_asset_ids": ["tbl_jan"]},
    )
    denied = service.enqueue(
        principal=Principal(subject_id="tenant", tenant_id="tenant-1", scopes=("knowledge:admin", "knowledge:space:space_sales")),
        correlation=Correlation("trace-semantic-denied"),
        request=request,
    )
    assert denied.error is not None
    assert denied.error.code.value == "permission_denied"
    try:
        SemanticDimensionAuthoringRequest(
            dimension_id="vehicle_series",
            space_id="space_sales",
            adapter="entity_crosswalk_v1",
            input_snapshot={"source_asset_ids": ["tbl_jan"], "source_path": "/etc/passwd"},
        )
    except ValueError as error:
        assert "unsafe" in str(error)
    else:
        raise AssertionError("unsafe snapshot must be rejected")
    with pytest.raises(ValueError, match="unsafe"):
        SemanticDimensionAuthoringRequest(
            dimension_id="vehicle_series",
            space_id="space_sales",
            adapter="entity_crosswalk_v1",
            input_snapshot={"source_asset_ids": ["tbl_jan"], "session_id": "legacy-session"},
        )


def test_semantic_job_decision_requeues_or_cancels_without_faking_publication(tmp_path: Path) -> None:
    from knowledge_platform.catalog import SqliteCatalogQueryRepository, SqliteSemanticDimensionJobWriter
    from knowledge_platform.semantic import (
        SemanticDimensionAuthoringRequest,
        SemanticDimensionAuthoringService,
        SemanticDimensionJobDecisionRequest,
        SemanticDimensionJobDecisionService,
    )

    database = tmp_path / "catalog.sqlite3"
    _create_catalog(database)
    writer = SqliteSemanticDimensionJobWriter(database)
    authoring = SemanticDimensionAuthoringService(
        catalog=SqliteCatalogQueryRepository(database),
        writer=writer,
    )
    request = SemanticDimensionAuthoringRequest(
        dimension_id="vehicle_series",
        space_id="space_sales",
        adapter="entity_crosswalk_v1",
        input_snapshot={"source_asset_ids": ["tbl_jan"]},
    )
    queued = authoring.enqueue(
        principal=_principal("knowledge:admin", "knowledge:space:space_sales"),
        correlation=Correlation("trace-enqueue-decision"),
        request=request,
    )
    job_id = queued.data["job"]["id"]
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE knowledge_authoring_jobs SET status = 'waiting_for_publish_confirmation', "
            "current_step = 'waiting_for_publish_confirmation', progress = 100 WHERE id = ?",
            (job_id,),
        )

    decisions = SemanticDimensionJobDecisionService(writer=writer)
    decision_request = SemanticDimensionJobDecisionRequest(
        job_id=job_id,
        space_id="space_sales",
        decision="confirm",
        expected_status="waiting_for_publish_confirmation",
    )
    confirmed = decisions.decide(
        principal=_principal("knowledge:admin", "knowledge:space:space_sales"),
        correlation=Correlation("trace-confirm-decision"),
        request=decision_request,
    )
    assert confirmed.status == "ok"
    assert confirmed.data["job"]["status"] == "queued"
    assert confirmed.data["job"]["published_uri"] == ""

    replay = decisions.decide(
        principal=_principal("knowledge:admin", "knowledge:space:space_sales"),
        correlation=Correlation("trace-confirm-replay"),
        request=decision_request,
    )
    assert replay.status == "ok"
    assert replay.data["job"]["status"] == "queued"
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM knowledge_authoring_events WHERE job_id = ? AND id LIKE 'authoring_decision_%'",
            (job_id,),
        ).fetchone()[0] == 1


def test_semantic_job_decision_does_not_accept_queued_job_as_a_stale_replay(tmp_path: Path) -> None:
    from knowledge_platform.catalog import SqliteCatalogQueryRepository, SqliteSemanticDimensionJobWriter
    from knowledge_platform.semantic import (
        SemanticDimensionAuthoringRequest,
        SemanticDimensionAuthoringService,
        SemanticDimensionJobDecisionRequest,
        SemanticDimensionJobDecisionService,
    )

    database = tmp_path / "catalog.sqlite3"
    _create_catalog(database)
    writer = SqliteSemanticDimensionJobWriter(database)
    authoring = SemanticDimensionAuthoringService(
        catalog=SqliteCatalogQueryRepository(database),
        writer=writer,
    )
    queued = authoring.enqueue(
        principal=_principal("knowledge:admin", "knowledge:space:space_sales"),
        correlation=Correlation("trace-stale-decision"),
        request=SemanticDimensionAuthoringRequest(
            dimension_id="vehicle_series",
            space_id="space_sales",
            adapter="entity_crosswalk_v1",
            input_snapshot={"source_asset_ids": ["tbl_jan"]},
        ),
    )
    decisions = SemanticDimensionJobDecisionService(writer=writer)
    result = decisions.decide(
        principal=_principal("knowledge:admin", "knowledge:space:space_sales"),
        correlation=Correlation("trace-stale-decision-request"),
        request=SemanticDimensionJobDecisionRequest(
            job_id=queued.data["job"]["id"],
            space_id="space_sales",
            decision="confirm",
            expected_status="waiting_for_publish_confirmation",
        ),
    )
    assert result.status == "error"
    assert result.error is not None
    assert result.error.code.value == "invalid_request"
