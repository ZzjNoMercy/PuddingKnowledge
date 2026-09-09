"""SQLite persistence for the local Catalog activation state machine."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .activation import CatalogActivationAuditEvent, CatalogActivationState


class SqliteCatalogActivationStore:
    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path.expanduser().absolute()
        if self._database_path.is_symlink():
            raise OSError("Catalog activation database must not be a symlink")
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection

    def _ensure_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS platform_catalog_activation_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    installation_id TEXT NOT NULL,
                    source_revision TEXT NOT NULL,
                    target_revision TEXT NOT NULL,
                    source_digest TEXT NOT NULL,
                    target_digest TEXT NOT NULL,
                    active_revision TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('pending', 'prepared', 'drained', 'verified', 'activated', 'rolled_back')),
                    source_read_only INTEGER NOT NULL CHECK (source_read_only IN (0, 1)),
                    target_read_only INTEGER NOT NULL CHECK (target_read_only IN (0, 1)),
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS platform_catalog_activation_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    installation_id TEXT NOT NULL,
                    source_revision TEXT NOT NULL,
                    target_revision TEXT NOT NULL,
                    active_revision TEXT NOT NULL,
                    detail_digest TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )

    def load(self) -> CatalogActivationState | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM platform_catalog_activation_state WHERE id = 1").fetchone()
        if row is None:
            return None
        return CatalogActivationState(
            installation_id=str(row["installation_id"]),
            source_revision=str(row["source_revision"]),
            target_revision=str(row["target_revision"]),
            source_digest=str(row["source_digest"]),
            target_digest=str(row["target_digest"]),
            active_revision=str(row["active_revision"]),
            status=str(row["status"]),
            source_read_only=bool(row["source_read_only"]),
            target_read_only=bool(row["target_read_only"]),
        )

    def save(self, state: CatalogActivationState) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO platform_catalog_activation_state (id, installation_id, source_revision, target_revision, source_digest, target_digest, active_revision, status, source_read_only, target_read_only, updated_at) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP) "
                "ON CONFLICT(id) DO UPDATE SET installation_id=excluded.installation_id, source_revision=excluded.source_revision, target_revision=excluded.target_revision, source_digest=excluded.source_digest, target_digest=excluded.target_digest, active_revision=excluded.active_revision, status=excluded.status, source_read_only=excluded.source_read_only, target_read_only=excluded.target_read_only, updated_at=CURRENT_TIMESTAMP",
                (
                    state.installation_id,
                    state.source_revision,
                    state.target_revision,
                    state.source_digest,
                    state.target_digest,
                    state.active_revision,
                    state.status,
                    int(state.source_read_only),
                    int(state.target_read_only),
                ),
            )

    def append_event(self, event: CatalogActivationAuditEvent) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO platform_catalog_activation_events (event_type, installation_id, source_revision, target_revision, active_revision, detail_digest, created_at) VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
                (
                    event.event_type,
                    event.installation_id,
                    event.source_revision,
                    event.target_revision,
                    event.active_revision,
                    event.detail_digest,
                ),
            )

    def event_types(self) -> tuple[str, ...]:
        with self._connect() as connection:
            rows = connection.execute("SELECT event_type FROM platform_catalog_activation_events ORDER BY id").fetchall()
        return tuple(str(row["event_type"]) for row in rows)


__all__ = ["SqliteCatalogActivationStore"]
