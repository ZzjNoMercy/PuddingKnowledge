"""SQLite fenced SyncRun store for local Connector Sync."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .ports import ConnectorSyncClaim, ConnectorSyncResult, SourceItemSnapshot

_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _key_digest(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise ValueError("Connector Sync idempotency key is invalid")
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _lease_is_live(value: str | None, *, now: datetime) -> bool:
    if not value:
        return False
    try:
        expires_at = datetime.fromisoformat(value)
    except ValueError:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at > now


class SqliteConnectorSyncStore:
    """Atomically claim a SyncRun and reconcile only its explicit source items."""

    def __init__(self, *, database_path: Path) -> None:
        self._database_path = database_path.expanduser().absolute()
        if self._database_path.is_symlink() or not self._database_path.is_file():
            raise FileNotFoundError("Connector Sync Catalog database is unavailable")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def claim(self, *, connector_id: str, space_id: str, idempotency_key: str) -> ConnectorSyncClaim:
        if not _ID_RE.fullmatch(connector_id) or not _ID_RE.fullmatch(space_id):
            raise ValueError("Connector Sync identity is invalid")
        key_digest = _key_digest(idempotency_key)
        run_id = "sync_process_" + key_digest.removeprefix("sha256:")[:48]
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connector = connection.execute(
                "SELECT space_id, status FROM knowledge_connectors WHERE id = ?",
                (connector_id,),
            ).fetchone()
            if connector is None or connector["space_id"] != space_id or connector["status"] not in {"ready", "active"}:
                raise ValueError("Connector is not available in the requested Space")
            row = connection.execute(
                "SELECT connector_id, status, lease_owner, lease_expires_at, attempt, stats_json FROM knowledge_sync_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if row is not None:
                if row["connector_id"] != connector_id:
                    raise ValueError("Connector Sync idempotency collision")
                if row["status"] == "succeeded":
                    try:
                        stats = json.loads(row["stats_json"] or "{}")
                    except json.JSONDecodeError as error:
                        raise ValueError("stored Connector Sync result is invalid") from error
                    if not isinstance(stats, dict):
                        raise ValueError("stored Connector Sync result is invalid")
                    return ConnectorSyncClaim(
                        acquired=False,
                        run_id=run_id,
                        existing_result=ConnectorSyncResult(
                            run_id=run_id,
                            connector_id=connector_id,
                            space_id=space_id,
                            discovered=int(stats.get("discovered") or 0),
                            changed=int(stats.get("changed") or 0),
                            unchanged=int(stats.get("unchanged") or 0),
                        ),
                    )
                if row["status"] == "running":
                    now = _now()
                    if _lease_is_live(row["lease_expires_at"], now=now):
                        return ConnectorSyncClaim(acquired=False, run_id=run_id)
                    owner = "connector-worker-" + secrets.token_hex(12)
                    connection.execute(
                        "UPDATE knowledge_sync_runs SET lease_owner = ?, lease_expires_at = ?, heartbeat_at = ?, "
                        "started_at = ?, finished_at = NULL, current_step = 'reconciling', progress = 0, "
                        "error_json = '{}', attempt = ?, updated_at = ? WHERE id = ? AND status = 'running'",
                        (owner, (now + timedelta(minutes=5)).isoformat(), now.isoformat(), now.isoformat(), int(row["attempt"] or 0) + 1, now.isoformat(), run_id),
                    )
                    return ConnectorSyncClaim(acquired=True, run_id=run_id, owner=owner)
                raise ValueError("stored Connector Sync has an unsupported status")
            owner = "connector-worker-" + secrets.token_hex(12)
            now = _now()
            connection.execute(
                """
                INSERT INTO knowledge_sync_runs
                    (id, connector_id, mode, status, cursor_json, stats_json, current_step, progress,
                     error_json, started_at, finished_at, lease_owner, lease_expires_at, heartbeat_at,
                     attempt, created_at, updated_at)
                VALUES (?, ?, 'incremental', 'running', ?, '{}', 'reconciling', 0, '{}', ?, NULL, ?, ?, ?, 1, ?, ?)
                """,
                (
                    run_id,
                    connector_id,
                    json.dumps({"idempotency_key_digest": key_digest}),
                    now.isoformat(),
                    owner,
                    (now + timedelta(minutes=5)).isoformat(),
                    now.isoformat(),
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            return ConnectorSyncClaim(acquired=True, run_id=run_id, owner=owner)

    def complete(
        self,
        *,
        run_id: str,
        owner: str,
        connector_id: str,
        space_id: str,
        snapshots: tuple[SourceItemSnapshot, ...],
    ) -> ConnectorSyncResult:
        if not _ID_RE.fullmatch(run_id) or not owner or not _ID_RE.fullmatch(connector_id) or not _ID_RE.fullmatch(space_id):
            raise ValueError("Connector Sync completion identity is invalid")
        if not snapshots or len({item.source_item_id for item in snapshots}) != len(snapshots):
            raise ValueError("Connector Sync snapshots must be non-empty and unique")
        if any(not _ID_RE.fullmatch(item.source_item_id) or not _DIGEST_RE.fullmatch(item.content_digest) or item.bytes < 0 for item in snapshots):
            raise ValueError("Connector Sync snapshot is invalid")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                "SELECT status, connector_id, lease_owner, lease_expires_at FROM knowledge_sync_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if (
                run is None
                or run["status"] != "running"
                or run["connector_id"] != connector_id
                or run["lease_owner"] != owner
                or not _lease_is_live(run["lease_expires_at"], now=_now())
            ):
                raise ValueError("Connector Sync lease is lost")
            connector = connection.execute(
                "SELECT space_id, status FROM knowledge_connectors WHERE id = ?",
                (connector_id,),
            ).fetchone()
            if connector is None or connector["space_id"] != space_id or connector["status"] not in {"ready", "active"}:
                raise ValueError("Connector is no longer available in the requested Space")
            changed = 0
            unchanged = 0
            for snapshot in snapshots:
                item = connection.execute(
                    "SELECT space_id, connector_id, asset_id, content_digest, status FROM knowledge_source_items WHERE id = ?",
                    (snapshot.source_item_id,),
                ).fetchone()
                if item is None or item["space_id"] != space_id or item["connector_id"] != connector_id or not item["asset_id"]:
                    raise ValueError("Connector source item is outside the requested binding")
                asset = connection.execute(
                    "SELECT space_id, content_digest FROM knowledge_assets WHERE id = ?",
                    (item["asset_id"],),
                ).fetchone()
                if asset is None or asset["space_id"] != space_id:
                    raise ValueError("Connector source Asset is outside the requested Space")
                if asset["content_digest"] != item["content_digest"]:
                    raise ValueError("Connector source item and Asset digests are inconsistent")
                if item["content_digest"] == snapshot.content_digest and item["status"] == "ready":
                    unchanged += 1
                else:
                    changed += 1
                connection.execute(
                    "UPDATE knowledge_source_items SET content_digest = ?, revision = ?, status = 'ready', "
                    "last_seen_sync_run_id = ?, updated_at = ? WHERE id = ?",
                    (snapshot.content_digest, snapshot.content_digest, run_id, _now().isoformat(), snapshot.source_item_id),
                )
                connection.execute(
                    "UPDATE knowledge_assets SET content_digest = ?, revision = ?, updated_at = ? WHERE id = ?",
                    (snapshot.content_digest, snapshot.content_digest, _now().isoformat(), item["asset_id"]),
                )
            connection.execute(
                "UPDATE knowledge_connectors SET last_sync_run_id = ?, last_synced_at = ?, updated_at = ? WHERE id = ?",
                (run_id, _now().isoformat(), _now().isoformat(), connector_id),
            )
            stats = {"discovered": len(snapshots), "changed": changed, "unchanged": unchanged, "failed": 0, "deleted": 0}
            connection.execute(
                "UPDATE knowledge_sync_runs SET status = 'succeeded', current_step = 'completed', progress = 100, "
                "stats_json = ?, finished_at = ?, updated_at = ?, lease_owner = NULL, lease_expires_at = NULL, heartbeat_at = NULL "
                "WHERE id = ? AND status = 'running' AND lease_owner = ?",
                (json.dumps(stats, sort_keys=True), _now().isoformat(), _now().isoformat(), run_id, owner),
            )
            return ConnectorSyncResult(run_id, connector_id, space_id, len(snapshots), changed, unchanged)

    def release(self, *, run_id: str, owner: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM knowledge_sync_runs WHERE id = ? AND status = 'running' AND lease_owner = ?",
                (run_id, owner),
            )
