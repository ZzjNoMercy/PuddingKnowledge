"""Application worker for explicit local Connector Sync."""

from __future__ import annotations

from .ports import (
    ConnectorSourceProvider,
    ConnectorSyncRequest,
    ConnectorSyncResult,
    ConnectorSyncStore,
)


class ConnectorSyncError(RuntimeError):
    """Connector Sync stopped before a fenced Catalog update."""


class ConnectorSyncWorker:
    """Coordinate source reads and a fenced SyncRun without legacy runtime state."""

    def __init__(self, *, source: ConnectorSourceProvider, store: ConnectorSyncStore) -> None:
        self._source = source
        self._store = store

    def sync(self, request: ConnectorSyncRequest) -> ConnectorSyncResult:
        claim = self._store.claim(
            connector_id=request.connector_id,
            space_id=request.space_id,
            idempotency_key=request.idempotency_key,
        )
        if not claim.acquired:
            if claim.existing_result is None:
                raise ConnectorSyncError("Connector Sync is already in progress")
            return claim.existing_result
        try:
            snapshots = tuple(
                self._source.read(source_item_id=item_id, path=path)
                for item_id, path in sorted(request.source_paths.items())
            )
            return self._store.complete(
                run_id=claim.run_id,
                owner=claim.owner,
                connector_id=request.connector_id,
                space_id=request.space_id,
                snapshots=snapshots,
            )
        except Exception:
            self._store.release(run_id=claim.run_id, owner=claim.owner)
            raise
