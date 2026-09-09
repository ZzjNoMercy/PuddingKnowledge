"""Durable local store for Phase 8 traffic policy rehearsals."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .traffic import TrafficAuditEvent, TrafficPolicyError, TrafficPolicySnapshot


class SqliteTrafficPolicyStore:
    """Persist traffic policies and digest-only events in one explicit SQLite file."""

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path.expanduser().absolute()
        if self._database_path.is_symlink():
            raise OSError("traffic policy database must not be a symlink")
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
                CREATE TABLE IF NOT EXISTS platform_traffic_policies (
                    capability TEXT PRIMARY KEY,
                    deployment_revision TEXT NOT NULL,
                    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
                    traffic_percent INTEGER NOT NULL CHECK (traffic_percent BETWEEN 0 AND 100),
                    healthy INTEGER NOT NULL CHECK (healthy IN (0, 1)),
                    rollback_deadline TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS platform_traffic_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    capability TEXT NOT NULL,
                    deployment_revision TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    key_digest TEXT NOT NULL,
                    detail_digest TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )

    def load(self, capability: str) -> TrafficPolicySnapshot | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT capability, deployment_revision, enabled, traffic_percent, healthy, rollback_deadline FROM platform_traffic_policies WHERE capability = ?",
                (capability,),
            ).fetchone()
        if row is None:
            return None
        try:
            from datetime import datetime

            return TrafficPolicySnapshot(
                capability=str(row["capability"]),
                deployment_revision=str(row["deployment_revision"]),
                enabled=bool(row["enabled"]),
                traffic_percent=int(row["traffic_percent"]),
                healthy=bool(row["healthy"]),
                rollback_deadline=datetime.fromisoformat(str(row["rollback_deadline"])),
            )
        except (TypeError, ValueError, TrafficPolicyError) as error:
            raise TrafficPolicyError("stored traffic policy is invalid") from error

    def save(self, policy: TrafficPolicySnapshot) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO platform_traffic_policies (capability, deployment_revision, enabled, traffic_percent, healthy, rollback_deadline, updated_at) VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP) "
                "ON CONFLICT(capability) DO UPDATE SET deployment_revision=excluded.deployment_revision, enabled=excluded.enabled, traffic_percent=excluded.traffic_percent, healthy=excluded.healthy, rollback_deadline=excluded.rollback_deadline, updated_at=CURRENT_TIMESTAMP",
                (
                    policy.capability,
                    policy.deployment_revision,
                    int(policy.enabled),
                    policy.traffic_percent,
                    int(policy.healthy),
                    policy.rollback_deadline.isoformat(),
                ),
            )

    def append_event(self, event: TrafficAuditEvent) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO platform_traffic_events (event_type, capability, deployment_revision, reason, key_digest, detail_digest, created_at) VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
                (
                    event.event_type,
                    event.capability,
                    event.deployment_revision,
                    event.reason,
                    event.key_digest,
                    event.detail_digest,
                ),
            )

    def event_types(self) -> tuple[str, ...]:
        with self._connect() as connection:
            rows = connection.execute("SELECT event_type FROM platform_traffic_events ORDER BY id").fetchall()
        return tuple(str(row["event_type"]) for row in rows)


__all__ = ["SqliteTrafficPolicyStore"]
