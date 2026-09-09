"""SQLite persistence for the single deployment active pointer."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .deployment import (
    DeploymentActivationAuditEvent,
    DeploymentActivationState,
    DeploymentManifest,
)

_SYSTEM_PATH_ALIASES = frozenset({Path("/private"), Path("/var")})


def _manifest_json(manifest: DeploymentManifest | None) -> str:
    if manifest is None:
        return ""
    return json.dumps(manifest.to_dict(), ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _manifest(value: str) -> DeploymentManifest | None:
    if not value:
        return None
    decoded = json.loads(value)
    if not isinstance(decoded, dict):
        raise ValueError("deployment manifest storage is invalid")
    return DeploymentManifest.from_dict(decoded)


class SqliteDeploymentActivationStore:
    def __init__(self, database_path: Path) -> None:
        requested_path = database_path.expanduser().absolute()
        if requested_path.is_symlink():
            raise OSError("deployment activation database path must not be a symlink")
        cursor = requested_path.parent
        while True:
            # macOS exposes the data volume through the system alias
            # /private; it is not a user-controlled traversal hop.
            if cursor.exists() and cursor.is_symlink() and cursor not in _SYSTEM_PATH_ALIASES:
                raise OSError("deployment activation database path must not contain symlinks")
            if cursor == cursor.parent:
                break
            cursor = cursor.parent
        self._database_path = requested_path.resolve()
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
                CREATE TABLE IF NOT EXISTS platform_deployment_activation_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    installation_id TEXT NOT NULL,
                    legacy_manifest_json TEXT NOT NULL,
                    candidate_manifest_json TEXT NOT NULL,
                    active_deployment_revision TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('pending', 'prepared', 'drained', 'verified', 'activated', 'rolled_back')),
                    legacy_read_only INTEGER NOT NULL CHECK (legacy_read_only IN (0, 1)),
                    candidate_read_only INTEGER NOT NULL CHECK (candidate_read_only IN (0, 1)),
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS platform_deployment_activation_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    active_deployment_revision TEXT NOT NULL,
                    detail_digest TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )

    def load(self) -> DeploymentActivationState | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM platform_deployment_activation_state WHERE id = 1").fetchone()
        if row is None:
            return None
        return DeploymentActivationState(
            installation_id=str(row["installation_id"]),
            legacy_manifest=_manifest(str(row["legacy_manifest_json"])),
            candidate_manifest=_manifest(str(row["candidate_manifest_json"])),
            active_deployment_revision=str(row["active_deployment_revision"]),
            status=str(row["status"]),
            legacy_read_only=bool(row["legacy_read_only"]),
            candidate_read_only=bool(row["candidate_read_only"]),
        )

    def save(self, state: DeploymentActivationState) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO platform_deployment_activation_state (id, installation_id, legacy_manifest_json, candidate_manifest_json, active_deployment_revision, status, legacy_read_only, candidate_read_only, updated_at) VALUES (1, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP) "
                "ON CONFLICT(id) DO UPDATE SET installation_id=excluded.installation_id, legacy_manifest_json=excluded.legacy_manifest_json, candidate_manifest_json=excluded.candidate_manifest_json, active_deployment_revision=excluded.active_deployment_revision, status=excluded.status, legacy_read_only=excluded.legacy_read_only, candidate_read_only=excluded.candidate_read_only, updated_at=CURRENT_TIMESTAMP",
                (
                    state.installation_id,
                    _manifest_json(state.legacy_manifest),
                    _manifest_json(state.candidate_manifest),
                    state.active_deployment_revision,
                    state.status,
                    int(state.legacy_read_only),
                    int(state.candidate_read_only),
                ),
            )

    def append_event(self, event: DeploymentActivationAuditEvent) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO platform_deployment_activation_events (event_type, active_deployment_revision, detail_digest, created_at) VALUES (?, ?, ?, CURRENT_TIMESTAMP)",
                (event.event_type, event.active_deployment_revision, event.detail_digest),
            )

    def event_types(self) -> tuple[str, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT event_type FROM platform_deployment_activation_events ORDER BY id"
            ).fetchall()
        return tuple(str(row["event_type"]) for row in rows)


__all__ = ["SqliteDeploymentActivationStore"]
