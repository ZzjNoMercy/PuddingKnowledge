from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from knowledge_contracts import Correlation, Principal, QueryError, QueryErrorCode, QueryResult
from knowledge_platform.catalog import SqliteLogicalDatasetProcessingJobStore, migrate_to_latest
from knowledge_platform.structured import (
    LogicalDatasetProcessingError,
    LogicalDatasetProcessingJobRequest,
    LogicalDatasetProcessingWorker,
)


def _catalog(path: Path) -> None:
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as connection:
        migrate_to_latest(connection)
    engine.dispose()


def _request(*, dataset_id: str = "dataset_local", space_id: str = "space_local", key: str = "job-1") -> LogicalDatasetProcessingJobRequest:
    return LogicalDatasetProcessingJobRequest(
        dataset_id=dataset_id,
        space_id=space_id,
        source_paths={"asset_source": Path("/explicit/source.csv")},
        idempotency_key=key,
        principal=Principal("worker-test", ("knowledge.processing", f"knowledge.space:{space_id}")),
        correlation=Correlation("logical-worker-test"),
    )


class _FakeProcessingService:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls = 0
        self.fail = fail

    async def process(self, *, principal: Principal, correlation: Correlation, request: object) -> QueryResult:
        del principal, correlation
        self.calls += 1
        if self.fail:
            raise RuntimeError("source profiler failed")
        return QueryResult(
            status="ok",
            trace_id="logical-worker-test",
            data={
                "dataset": {
                    "source_uri": "knowledge://spaces/space_local/structured-assets/dataset_local/source",
                    "content_digest": "sha256:" + "a" * 64,
                    "row_count": 42,
                }
            },
        )


def test_logical_dataset_worker_claims_completes_and_replays_without_second_service_call(tmp_path: Path) -> None:
    catalog = tmp_path / "catalog.sqlite3"
    _catalog(catalog)
    service = _FakeProcessingService()
    worker = LogicalDatasetProcessingWorker(
        service=service,
        jobs=SqliteLogicalDatasetProcessingJobStore(database_path=catalog),
    )
    request = _request()

    first = asyncio.run(worker.process(request))
    second = asyncio.run(worker.process(request))

    assert first == second
    assert service.calls == 1
    assert first.row_count == 42
    with sqlite3.connect(catalog) as connection:
        row = connection.execute(
            "SELECT status, current_step, progress, lease_owner, input_uri, metadata_json "
            "FROM knowledge_processing_jobs WHERE id = ?",
            (first.job_id,),
        ).fetchone()
    assert row[:4] == ("succeeded", "completed", 100, None)
    assert row[4].startswith("knowledge://")
    assert "source.csv" not in row[5]
    assert "explicit" not in row[5]


def test_logical_dataset_worker_releases_failed_claim_and_rejects_cross_space_reuse(tmp_path: Path) -> None:
    catalog = tmp_path / "catalog.sqlite3"
    _catalog(catalog)
    service = _FakeProcessingService(fail=True)
    worker = LogicalDatasetProcessingWorker(
        service=service,
        jobs=SqliteLogicalDatasetProcessingJobStore(database_path=catalog),
    )
    with pytest.raises(RuntimeError, match="profiler"):
        asyncio.run(worker.process(_request(key="failed")))
    with sqlite3.connect(catalog) as connection:
        assert connection.execute("SELECT COUNT(*) FROM knowledge_processing_jobs").fetchone()[0] == 0

    store = SqliteLogicalDatasetProcessingJobStore(database_path=catalog)
    first = store.claim(
        dataset_id="dataset_local", space_id="space_local", idempotency_key="cross-space", binding_digest=_request(key="x").binding_digest()
    )
    assert first[0]
    with pytest.raises(ValueError, match="collision"):
        store.claim(
            dataset_id="dataset_local", space_id="space_other", idempotency_key="cross-space", binding_digest=_request(space_id="space_other", key="x").binding_digest()
        )
    store.release(job_id=first[1], owner=first[2])


def test_logical_dataset_worker_rejects_non_ok_service_result_and_keeps_key_digest_only(tmp_path: Path) -> None:
    catalog = tmp_path / "catalog.sqlite3"
    _catalog(catalog)

    class ErrorService:
        async def process(self, *, principal: Principal, correlation: Correlation, request: object) -> QueryResult:
            del principal, correlation, request
            return QueryResult(
                status="error",
                trace_id="logical-worker-test",
                error=QueryError(QueryErrorCode.BINDING_UNAVAILABLE, "source is not ready"),
            )

    worker = LogicalDatasetProcessingWorker(
        service=ErrorService(),
        jobs=SqliteLogicalDatasetProcessingJobStore(database_path=catalog),
    )
    with pytest.raises(LogicalDatasetProcessingError):
        asyncio.run(worker.process(_request(key="error")))
    raw = catalog.read_bytes()
    assert b"job-1" not in raw
    assert b"/explicit/source.csv" not in raw


def test_logical_dataset_job_store_fences_terminal_uri_identity(tmp_path: Path) -> None:
    catalog = tmp_path / "catalog.sqlite3"
    _catalog(catalog)
    store = SqliteLogicalDatasetProcessingJobStore(database_path=catalog)
    request = _request(key="uri-fence")
    acquired, job_id, owner, existing = store.claim(
        dataset_id=request.dataset_id,
        space_id=request.space_id,
        idempotency_key=request.idempotency_key,
        binding_digest=request.binding_digest(),
    )
    assert acquired and existing is None
    with pytest.raises(ValueError, match="resource URI"):
        store.complete(
            job_id=job_id,
            owner=owner,
            dataset_id=request.dataset_id,
            space_id=request.space_id,
            binding_digest=request.binding_digest(),
            resource_uri="knowledge://spaces/space_other/structured-assets/dataset_local/source",
            content_digest="sha256:" + "a" * 64,
            row_count=1,
        )
    store.release(job_id=job_id, owner=owner)
