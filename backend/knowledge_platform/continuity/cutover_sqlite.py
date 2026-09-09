"""Durable SQLite state for the ordered local cutover coordinator."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .cutover import CutoverAuditEvent, CutoverUnitState


class SqliteCutoverStateStore:
    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path.expanduser().absolute()
        if self._database_path.is_symlink():
            raise OSError("cutover state database must not be a symlink")
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
                CREATE TABLE IF NOT EXISTS platform_cutover_units (
                    unit TEXT PRIMARY KEY,
                    status TEXT NOT NULL CHECK (status IN ('pending', 'active', 'stable', 'rolled_back')),
                    deployment_revision TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS platform_cutover_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    unit TEXT NOT NULL,
                    deployment_revision TEXT NOT NULL,
                    detail_digest TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )

    def load(self, unit: str) -> CutoverUnitState | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT unit, status, deployment_revision FROM platform_cutover_units WHERE unit = ?",
                (unit,),
            ).fetchone()
        if row is None:
            return None
        return CutoverUnitState(
            unit=str(row["unit"]),
            status=str(row["status"]),
            deployment_revision=str(row["deployment_revision"]),
        )

    def save(self, state: CutoverUnitState) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO platform_cutover_units (unit, status, deployment_revision, updated_at) VALUES (?, ?, ?, CURRENT_TIMESTAMP) "
                "ON CONFLICT(unit) DO UPDATE SET status=excluded.status, deployment_revision=excluded.deployment_revision, updated_at=CURRENT_TIMESTAMP",
                (state.unit, state.status, state.deployment_revision),
            )

    def append_event(self, event: CutoverAuditEvent) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO platform_cutover_events (event_type, unit, deployment_revision, detail_digest, created_at) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)",
                (event.event_type, event.unit, event.deployment_revision, event.detail_digest),
            )

    def event_types(self) -> tuple[str, ...]:
        with self._connect() as connection:
            rows = connection.execute("SELECT event_type FROM platform_cutover_events ORDER BY id").fetchall()
        return tuple(str(row["event_type"]) for row in rows)


__all__ = ["SqliteCutoverStateStore"]
