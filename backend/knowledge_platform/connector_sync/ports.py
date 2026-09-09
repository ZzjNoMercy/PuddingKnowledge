"""Framework-neutral ports for Connector Sync processing."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ConnectorSyncRequest:
    connector_id: str
    space_id: str
    source_paths: dict[str, Path]
    idempotency_key: str

    def __post_init__(self) -> None:
        if not self.connector_id.strip() or not self.space_id.strip() or not self.idempotency_key.strip():
            raise ValueError("ConnectorSyncRequest identity must not be empty")
        if not self.source_paths:
            raise ValueError("ConnectorSyncRequest.source_paths must not be empty")


@dataclass(frozen=True, slots=True)
class SourceItemSnapshot:
    source_item_id: str
    content_digest: str
    bytes: int


@dataclass(frozen=True, slots=True)
class ConnectorSyncClaim:
    acquired: bool
    run_id: str
    owner: str = ""
    existing_result: ConnectorSyncResult | None = None


@dataclass(frozen=True, slots=True)
class ConnectorSyncResult:
    run_id: str
    connector_id: str
    space_id: str
    discovered: int
    changed: int
    unchanged: int


class ConnectorSourceProvider(Protocol):
    def read(self, *, source_item_id: str, path: Path) -> SourceItemSnapshot: ...


class ConnectorSyncStore(Protocol):
    def claim(self, *, connector_id: str, space_id: str, idempotency_key: str) -> ConnectorSyncClaim: ...

    def complete(
        self,
        *,
        run_id: str,
        owner: str,
        connector_id: str,
        space_id: str,
        snapshots: tuple[SourceItemSnapshot, ...],
    ) -> ConnectorSyncResult: ...

    def release(self, *, run_id: str, owner: str) -> None: ...
