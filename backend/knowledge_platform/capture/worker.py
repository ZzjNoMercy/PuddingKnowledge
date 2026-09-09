"""Application worker for explicit local Web Capture processing."""

from __future__ import annotations

from knowledge_contracts import is_valid_knowledge_uri

from .ports import (
    CaptureProcessingJobStore,
    CaptureProcessingRequest,
    CaptureProcessingResult,
    CapturePublishingService,
    CaptureSnapshotRepository,
)


class CaptureProcessingError(RuntimeError):
    """Capture processing stopped before publishing a validated content asset."""


class CaptureProcessingWorker:
    """Coordinate capture ports without public Tool, Graph, or Session access."""

    def __init__(
        self,
        *,
        snapshots: CaptureSnapshotRepository,
        publisher: CapturePublishingService,
        jobs: CaptureProcessingJobStore,
    ) -> None:
        self._snapshots = snapshots
        self._publisher = publisher
        self._jobs = jobs

    async def process(self, request: CaptureProcessingRequest) -> CaptureProcessingResult:
        claim = await self._jobs.claim(idempotency_key=request.idempotency_key)
        if not claim.acquired:
            if claim.existing_resource_uri is None:
                raise CaptureProcessingError("Capture processing is already in progress")
            return CaptureProcessingResult(request.asset_id, request.source_revision, claim.existing_resource_uri)
        try:
            snapshot = await self._snapshots.get(snapshot_id=request.asset_id, source_revision=request.source_revision)
            if (snapshot.snapshot_id, snapshot.source_revision) != (request.asset_id, request.source_revision):
                raise CaptureProcessingError("Capture Snapshot identity does not match processing request")
            if (snapshot.source_uri, snapshot.content_digest) != (request.source_uri, request.content_digest):
                raise CaptureProcessingError("Capture source fingerprint does not match processing request")
            resource_uri = await self._publisher.publish(snapshot=snapshot, idempotency_key=request.idempotency_key)
            if not is_valid_knowledge_uri(resource_uri):
                raise CaptureProcessingError("Capture publisher returned a non-portable resource URI")
            await self._jobs.complete(idempotency_key=request.idempotency_key, resource_uri=resource_uri)
            return CaptureProcessingResult(snapshot.snapshot_id, snapshot.source_revision, resource_uri)
        except Exception:
            await self._jobs.release(idempotency_key=request.idempotency_key)
            raise
