from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from knowledge_platform.capture import (
    CaptureProcessingRequest,
    CaptureProcessingWorker,
    LocalCapturePublishingService,
    SqliteCaptureProcessingJobStore,
)
from knowledge_platform.catalog import migrate_to_latest
from knowledge_platform.wiki import LocalImmutableRawSnapshotRepository


def _digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _catalog(path: Path) -> None:
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as connection:
        migrate_to_latest(connection)
    engine.dispose()


def _request(source: Path, *, space_id: str = "space_local", asset_id: str = "asset_capture") -> CaptureProcessingRequest:
    digest = _digest(source)
    return CaptureProcessingRequest(
        asset_id=asset_id,
        source_revision=digest,
        source_uri=f"knowledge://spaces/{space_id}/assets/{asset_id}",
        content_digest=digest,
        idempotency_key="capture-local-1",
    )


def test_capture_worker_is_bounded_idempotent_and_linted(tmp_path: Path) -> None:
    source = tmp_path / "source.md"
    source.write_text("# Captured\n\nLocal capture body.", encoding="utf-8")
    catalog = tmp_path / "catalog.sqlite3"
    _catalog(catalog)
    request = _request(source)
    worker = CaptureProcessingWorker(
        snapshots=LocalImmutableRawSnapshotRepository(
            snapshot_root=tmp_path / "raw",
            snapshot_id=request.asset_id,
            source_revision=request.source_revision,
            source_uri=request.source_uri,
            path=source,
            expected_digest=request.content_digest,
        ),
        publisher=LocalCapturePublishingService(root=tmp_path / "published", space_id="space_local"),
        jobs=SqliteCaptureProcessingJobStore(database_path=catalog, space_id="space_local"),
    )

    first = asyncio.run(worker.process(request))
    second = asyncio.run(worker.process(request))

    assert first == second
    assert first.resource_uri.endswith("/captures/asset_capture/content")
    assert (tmp_path / "published" / "captures" / "asset_capture.md").is_file()


def test_capture_worker_releases_failed_claim_and_rejects_digest_drift(tmp_path: Path) -> None:
    source = tmp_path / "source.md"
    source.write_text("# Captured", encoding="utf-8")
    catalog = tmp_path / "catalog.sqlite3"
    _catalog(catalog)
    request = _request(source)
    source.write_text("# Changed", encoding="utf-8")
    worker = CaptureProcessingWorker(
        snapshots=LocalImmutableRawSnapshotRepository(
            snapshot_root=tmp_path / "raw",
            snapshot_id=request.asset_id,
            source_revision=request.source_revision,
            source_uri=request.source_uri,
            path=source,
            expected_digest=request.content_digest,
        ),
        publisher=LocalCapturePublishingService(root=tmp_path / "published", space_id="space_local"),
        jobs=SqliteCaptureProcessingJobStore(database_path=catalog, space_id="space_local"),
    )

    with pytest.raises(ValueError, match="digest"):
        asyncio.run(worker.process(request))
    with sqlite3.connect(catalog) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM knowledge_processing_jobs WHERE kind = 'read_later_capture'"
        ).fetchone()[0] == 0


def test_capture_job_store_does_not_cross_space_replay(tmp_path: Path) -> None:
    catalog = tmp_path / "catalog.sqlite3"
    _catalog(catalog)
    first = SqliteCaptureProcessingJobStore(database_path=catalog, space_id="space_a")
    other = SqliteCaptureProcessingJobStore(database_path=catalog, space_id="space_b")
    assert asyncio.run(first.claim(idempotency_key="same-capture-key")).acquired is True
    asyncio.run(first.complete(
        idempotency_key="same-capture-key",
        resource_uri="knowledge://spaces/space_a/captures/asset/content",
    ))
    with pytest.raises(ValueError, match="another Space"):
        asyncio.run(other.claim(idempotency_key="same-capture-key"))
