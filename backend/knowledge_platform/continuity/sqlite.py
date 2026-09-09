"""Restartable local sidecar state with explicit legacy rollback fencing."""

from __future__ import annotations

import hashlib
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .ports import (
    CONTINUITY_CAPABILITIES,
    CapabilityHandlers,
    ContinuityAuditEvent,
    ContinuityRequest,
    ContinuityResult,
)


class SqliteContinuityError(RuntimeError):
    """A durable continuity state transition cannot safely proceed."""


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC)


def _live(value: str | None, now: datetime) -> bool:
    if not value:
        return False
    try:
        return datetime.fromisoformat(value) > now
    except ValueError:
        return False


class SqlitePlatformSidecar:
    """A finite, restartable sidecar rehearsal backed by an explicit SQLite file."""

    def __init__(self, *, database_path: Path, handlers: CapabilityHandlers, lease_seconds: int = 300) -> None:
        unknown = set(handlers) - set(CONTINUITY_CAPABILITIES)
        if unknown:
            raise ValueError("handlers contain unsupported capabilities")
        if lease_seconds <= 0 or lease_seconds > 86_400:
            raise ValueError("continuity lease duration is invalid")
        self._database_path = database_path.expanduser().absolute()
        if self._database_path.is_symlink():
            raise OSError("continuity database must not be a symlink")
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        self._handlers = dict(handlers)
        self._lease_seconds = lease_seconds
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection

    def _ensure_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS platform_continuity_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    legacy_enabled INTEGER NOT NULL CHECK (legacy_enabled IN (0, 1)),
                    sidecar_active INTEGER NOT NULL CHECK (sidecar_active IN (0, 1)),
                    active_revision TEXT NOT NULL,
                    rollback_revision TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                INSERT OR IGNORE INTO platform_continuity_state
                    (id, legacy_enabled, sidecar_active, active_revision, rollback_revision, updated_at)
                    VALUES (1, 1, 0, '', 'legacy', CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS platform_continuity_runs (
                    key_digest TEXT PRIMARY KEY,
                    capability TEXT NOT NULL,
                    space_id TEXT NOT NULL,
                    deployment_revision TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'released', 'rolled_back')),
                    owner TEXT NOT NULL,
                    lease_expires_at TEXT,
                    resource_uri TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS platform_continuity_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    capability TEXT NOT NULL,
                    space_id TEXT NOT NULL,
                    deployment_revision TEXT NOT NULL,
                    idempotency_digest TEXT NOT NULL,
                    detail_digest TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )

    def _state(self, connection: sqlite3.Connection) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM platform_continuity_state WHERE id = 1").fetchone()
        if row is None:
            raise SqliteContinuityError("continuity state is missing")
        return row

    @property
    def legacy_enabled(self) -> bool:
        with self._connect() as connection:
            return bool(self._state(connection)["legacy_enabled"])

    @property
    def active(self) -> bool:
        with self._connect() as connection:
            return bool(self._state(connection)["sidecar_active"])

    @property
    def active_revision(self) -> str:
        with self._connect() as connection:
            return str(self._state(connection)["active_revision"])

    def _event(
        self,
        connection: sqlite3.Connection,
        *,
        event_type: str,
        capability: str,
        space_id: str,
        revision: str,
        key_digest: str = "",
        detail_digest: str = "",
    ) -> None:
        connection.execute(
            "INSERT INTO platform_continuity_events (event_type, capability, space_id, deployment_revision, idempotency_digest, detail_digest, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (event_type, capability, space_id, revision, key_digest, detail_digest, _now().isoformat()),
        )

    def stop_legacy(self) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state = self._state(connection)
            if state["sidecar_active"]:
                raise SqliteContinuityError("cannot stop legacy while sidecar is active")
            connection.execute("UPDATE platform_continuity_state SET legacy_enabled = 0, updated_at = ? WHERE id = 1", (_now().isoformat(),))
            self._event(connection, event_type="legacy_stopped", capability="continuity", space_id="system", revision="legacy")

    def activate(self, *, deployment_revision: str) -> None:
        if not deployment_revision or len(deployment_revision) > 160:
            raise ValueError("deployment revision is invalid")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state = self._state(connection)
            if state["legacy_enabled"]:
                raise SqliteContinuityError("legacy workers must be stopped before sidecar activation")
            if state["sidecar_active"] and state["active_revision"] != deployment_revision:
                raise SqliteContinuityError("a sidecar cannot switch revisions while active")
            connection.execute(
                "UPDATE platform_continuity_state SET sidecar_active = 1, active_revision = ?, updated_at = ? WHERE id = 1",
                (deployment_revision, _now().isoformat()),
            )
            self._event(
                connection,
                event_type="sidecar_activated",
                capability="continuity",
                space_id="system",
                revision=deployment_revision,
            )

    @staticmethod
    def _key_digest(request: ContinuityRequest) -> str:
        return _digest("|".join((request.capability, request.space_id, request.resource_key, request.idempotency_key)))

    def _claim(self, request: ContinuityRequest) -> tuple[str, ContinuityResult | None]:
        key_digest = self._key_digest(request)
        owner = "sidecar-" + uuid.uuid4().hex
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state = self._state(connection)
            if not state["sidecar_active"] or state["active_revision"] != request.deployment_revision:
                raise SqliteContinuityError("request revision is not active")
            row = connection.execute("SELECT * FROM platform_continuity_runs WHERE key_digest = ?", (key_digest,)).fetchone()
            if row is not None:
                if str(row["deployment_revision"]) != request.deployment_revision:
                    raise SqliteContinuityError("idempotency key is bound to another deployment revision")
                if row["status"] == "completed":
                    return "", ContinuityResult(
                        capability=str(row["capability"]),
                        space_id=str(row["space_id"]),
                        resource_uri=str(row["resource_uri"]),
                        deployment_revision=str(row["deployment_revision"]),
                        replayed=True,
                    )
                if row["status"] == "running" and _live(row["lease_expires_at"], now):
                    raise SqliteContinuityError("continuity run is already leased")
            connection.execute(
                "INSERT INTO platform_continuity_runs (key_digest, capability, space_id, deployment_revision, status, owner, lease_expires_at, resource_uri, updated_at) VALUES (?, ?, ?, ?, 'running', ?, ?, '', ?) "
                "ON CONFLICT(key_digest) DO UPDATE SET capability=excluded.capability, space_id=excluded.space_id, deployment_revision=excluded.deployment_revision, status='running', owner=excluded.owner, lease_expires_at=excluded.lease_expires_at, resource_uri='', updated_at=excluded.updated_at",
                (key_digest, request.capability, request.space_id, request.deployment_revision, owner, (now + timedelta(seconds=self._lease_seconds)).isoformat(), now.isoformat()),
            )
            self._event(
                connection,
                event_type="capability_claimed",
                capability=request.capability,
                space_id=request.space_id,
                revision=request.deployment_revision,
                key_digest=key_digest,
            )
        return owner, None

    def _complete(self, request: ContinuityRequest, *, owner: str, resource_uri: str) -> ContinuityResult:
        if not resource_uri.startswith(f"knowledge://spaces/{request.space_id}/"):
            raise SqliteContinuityError("capability result is outside request Space")
        key_digest = self._key_digest(request)
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state = self._state(connection)
            row = connection.execute("SELECT * FROM platform_continuity_runs WHERE key_digest = ?", (key_digest,)).fetchone()
            if (
                not state["sidecar_active"]
                or state["active_revision"] != request.deployment_revision
                or row is None
                or row["status"] != "running"
                or row["owner"] != owner
                or not _live(row["lease_expires_at"], now)
            ):
                raise SqliteContinuityError("continuity run lease is lost")
            connection.execute(
                "UPDATE platform_continuity_runs SET status='completed', lease_expires_at=NULL, resource_uri=?, updated_at=? WHERE key_digest=? AND status='running' AND owner=?",
                (resource_uri, now.isoformat(), key_digest, owner),
            )
            self._event(
                connection,
                event_type="capability_completed",
                capability=request.capability,
                space_id=request.space_id,
                revision=request.deployment_revision,
                key_digest=key_digest,
            )
        return ContinuityResult(
            capability=request.capability,
            space_id=request.space_id,
            resource_uri=resource_uri,
            deployment_revision=request.deployment_revision,
        )

    def _release(self, request: ContinuityRequest, *, owner: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE platform_continuity_runs SET status='released', lease_expires_at=NULL, updated_at=? WHERE key_digest=? AND status='running' AND owner=?",
                (_now().isoformat(), self._key_digest(request), owner),
            )

    def process(self, request: ContinuityRequest) -> ContinuityResult:
        handler = self._handlers.get(request.capability)
        if handler is None:
            raise SqliteContinuityError("capability is not enabled in this sidecar")
        owner, prior = self._claim(request)
        if prior is not None:
            return prior
        try:
            resource_uri = handler(request)
            return self._complete(request, owner=owner, resource_uri=resource_uri)
        except Exception:
            self._release(request, owner=owner)
            raise

    def rollback(self, *, reason: str) -> None:
        if not reason.strip():
            raise ValueError("rollback reason must not be empty")
        reason_digest = _digest(reason)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state = self._state(connection)
            prior_revision = str(state["active_revision"])
            connection.execute(
                "UPDATE platform_continuity_runs SET status='rolled_back', lease_expires_at=NULL, updated_at=? WHERE status='running'",
                (_now().isoformat(),),
            )
            connection.execute(
                "UPDATE platform_continuity_state SET sidecar_active=0, active_revision='', legacy_enabled=1, rollback_revision=?, updated_at=? WHERE id=1",
                (prior_revision or "legacy", _now().isoformat()),
            )
            self._event(
                connection,
                event_type="rolled_back_to_legacy",
                capability="continuity",
                space_id="system",
                revision="legacy",
                detail_digest=reason_digest,
            )

    def audit_events(self) -> tuple[ContinuityAuditEvent, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT event_type, capability, space_id, deployment_revision, idempotency_digest, detail_digest FROM platform_continuity_events ORDER BY id"
            ).fetchall()
        return tuple(
            ContinuityAuditEvent(
                event_type=str(row["event_type"]),
                capability=str(row["capability"]),
                space_id=str(row["space_id"]),
                deployment_revision=str(row["deployment_revision"]),
                idempotency_digest=str(row["idempotency_digest"]),
                detail_digest=str(row["detail_digest"]),
            )
            for row in rows
        )


__all__ = ["SqliteContinuityError", "SqlitePlatformSidecar"]
