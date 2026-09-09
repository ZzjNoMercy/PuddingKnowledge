"""Fenced SQLite store for Semantic Dimension staging jobs."""

from __future__ import annotations

import json
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from knowledge_platform.semantic.worker import (
    SemanticDimensionBuildArtifact,
    SemanticDimensionBuildClaim,
    SemanticDimensionBuildInput,
    SemanticDimensionPublication,
)

_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,160}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _live(value: str | None, now: datetime) -> bool:
    if not value:
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed > now


class SqliteSemanticDimensionJobStore:
    """Claim queued authoring jobs and persist only portable staging metadata."""

    def __init__(self, *, database_path: Path) -> None:
        self._database_path = database_path.expanduser().absolute()
        if self._database_path.is_symlink() or not self._database_path.is_file():
            raise FileNotFoundError("semantic dimension Catalog database is unavailable")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @staticmethod
    def _space_from_scope(scope_uri: str, dimension_id: str) -> str:
        prefix = "knowledge://spaces/"
        suffix = f"/semantic-dimensions/{dimension_id}"
        if not scope_uri.startswith(prefix) or not scope_uri.endswith(suffix):
            raise ValueError("semantic dimension scope URI is not canonical")
        space_id = scope_uri[len(prefix) : -len(suffix)]
        if not _ID_RE.fullmatch(space_id):
            raise ValueError("semantic dimension Space is invalid")
        return space_id

    @staticmethod
    def _sources(connection: sqlite3.Connection, *, space_id: str, source_snapshot: object) -> tuple[dict[str, str], ...]:
        if not isinstance(source_snapshot, list) or not source_snapshot:
            raise ValueError("semantic dimension source snapshot is invalid")
        normalized: list[dict[str, str]] = []
        for item in source_snapshot:
            if not isinstance(item, dict) or set(item) != {"asset_id", "content_digest"}:
                raise ValueError("semantic dimension source snapshot is invalid")
            asset_id = str(item["asset_id"])
            content_digest = str(item["content_digest"])
            if not _ID_RE.fullmatch(asset_id) or not _DIGEST_RE.fullmatch(content_digest):
                raise ValueError("semantic dimension source snapshot is unsafe")
            row = connection.execute(
                "SELECT space_id, content_digest, reference_status, capabilities FROM knowledge_structured_assets WHERE id = ?",
                (asset_id,),
            ).fetchone()
            if row is None or row["space_id"] != space_id or row["content_digest"] != content_digest or row["reference_status"] not in {"ready", "verified", "active"}:
                raise ValueError("semantic dimension source is no longer approved")
            try:
                capabilities = json.loads(row["capabilities"] or "[]")
            except json.JSONDecodeError as error:
                raise ValueError("semantic dimension source capabilities are invalid") from error
            if not isinstance(capabilities, list) or "table_query" not in {str(value) for value in capabilities}:
                raise ValueError("semantic dimension source lacks table_query capability")
            normalized.append({"asset_id": asset_id, "content_digest": content_digest})
        if len({item["asset_id"] for item in normalized}) != len(normalized):
            raise ValueError("semantic dimension source snapshot is duplicated")
        return tuple(normalized)

    def claim(self, *, job_id: str, space_id: str) -> SemanticDimensionBuildClaim:
        if not _ID_RE.fullmatch(job_id) or not _ID_RE.fullmatch(space_id):
            raise ValueError("semantic dimension job identity is invalid")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT kind, dimension_id, adapter, scope_uri, input_snapshot_json, status, staging_uri, lease_owner, lease_expires_at, attempt "
                "FROM knowledge_authoring_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if row is None or row["kind"] != "semantic_dimension_build":
                raise LookupError("semantic dimension job does not exist")
            if self._space_from_scope(str(row["scope_uri"]), str(row["dimension_id"])) != space_id:
                raise PermissionError("semantic dimension job belongs to another Space")
            status = str(row["status"] or "")
            if status == "running" and _live(row["lease_expires_at"], _now()):
                return SemanticDimensionBuildClaim(False)
            if status != "queued" and not (status == "running"):
                return SemanticDimensionBuildClaim(False)
            try:
                snapshot_record = json.loads(row["input_snapshot_json"] or "{}")
            except json.JSONDecodeError as error:
                raise ValueError("semantic dimension input snapshot is invalid") from error
            if not isinstance(snapshot_record, dict):
                raise ValueError("semantic dimension input snapshot is invalid")
            source_snapshot = self._sources(connection, space_id=space_id, source_snapshot=snapshot_record.get("source_snapshot"))
            now = _now()
            owner = "semantic-worker-" + secrets.token_hex(12)
            connection.execute(
                "UPDATE knowledge_authoring_jobs SET status = 'running', current_step = 'processing', progress = 10, "
                "started_at = ?, finished_at = NULL, lease_owner = ?, lease_expires_at = ?, heartbeat_at = ?, "
                "error_message = '', attempt = ?, updated_at = ? WHERE id = ? AND status IN ('queued', 'running')",
                (now.isoformat(), owner, (now + timedelta(minutes=5)).isoformat(), now.isoformat(), int(row["attempt"] or 0) + 1, now.isoformat(), job_id),
            )
            return SemanticDimensionBuildClaim(
                acquired=True,
                owner=owner,
                build_input=SemanticDimensionBuildInput(
                    job_id=job_id,
                    space_id=space_id,
                    dimension_id=str(row["dimension_id"]),
                    adapter=str(row["adapter"]),
                    source_snapshot=source_snapshot,
                    publish_after_confirmation=bool(str(row["staging_uri"] or "")),
                ),
            )

    def complete(
        self,
        *,
        job_id: str,
        owner: str,
        build_input: SemanticDimensionBuildInput,
        artifact: SemanticDimensionBuildArtifact,
        publication: SemanticDimensionPublication | None = None,
    ) -> None:
        expected_uri = f"knowledge://spaces/{build_input.space_id}/semantic-dimensions/{build_input.dimension_id}/staging"
        if artifact.staging_uri != expected_uri or not _DIGEST_RE.fullmatch(artifact.staging_digest):
            raise ValueError("semantic dimension staging identity is invalid")
        summary = dict(artifact.result_summary)
        summary_snapshot = summary.get("source_snapshot")
        if summary_snapshot != [dict(item) for item in build_input.source_snapshot]:
            raise ValueError("semantic dimension staging lineage is invalid")
        if build_input.publish_after_confirmation:
            expected_published_uri = f"knowledge://spaces/{build_input.space_id}/semantic-dimensions/{build_input.dimension_id}/published"
            if publication is None or publication.published_uri != expected_published_uri:
                raise ValueError("semantic dimension publication is missing or mis-bound")
        elif publication is not None:
            raise ValueError("semantic dimension cannot publish before confirmation")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT scope_uri, dimension_id, status, lease_owner, lease_expires_at, input_snapshot_json FROM knowledge_authoring_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if row is None or row["status"] != "running" or row["lease_owner"] != owner or not _live(row["lease_expires_at"], _now()):
                raise ValueError("semantic dimension worker lease is lost")
            if self._space_from_scope(str(row["scope_uri"]), str(row["dimension_id"])) != build_input.space_id or str(row["dimension_id"]) != build_input.dimension_id:
                raise ValueError("semantic dimension worker binding is invalid")
            try:
                snapshot_record = json.loads(row["input_snapshot_json"] or "{}")
            except json.JSONDecodeError as error:
                raise ValueError("semantic dimension input snapshot is invalid") from error
            self._sources(connection, space_id=build_input.space_id, source_snapshot=snapshot_record.get("source_snapshot") if isinstance(snapshot_record, dict) else None)
            now = _now()
            next_status = "published" if publication is not None else "waiting_for_publish_confirmation"
            next_step = "published" if publication is not None else "waiting_for_publish_confirmation"
            next_progress = 100 if publication is not None else 90
            next_published_uri = publication.published_uri if publication is not None else ""
            next_published_digest = publication.published_digest if publication is not None else ""
            connection.execute(
                "UPDATE knowledge_authoring_jobs SET status = ?, current_step = ?, progress = ?, "
                "staging_uri = ?, staging_reference_digest = ?, published_uri = ?, published_reference_digest = ?, result_summary_json = ?, updated_at = ?, lease_owner = NULL, "
                "lease_expires_at = NULL, heartbeat_at = NULL WHERE id = ? AND status = 'running' AND lease_owner = ?",
                (next_status, next_step, next_progress, artifact.staging_uri, artifact.staging_digest, next_published_uri, next_published_digest, json.dumps(summary, ensure_ascii=False, sort_keys=True), now.isoformat(), job_id, owner),
            )
            connection.execute(
                "INSERT OR IGNORE INTO knowledge_authoring_events (id, job_id, level, message, metadata_json, created_at) VALUES (?, ?, 'info', ?, ?, ?)",
                (
                    f"{job_id}_staged_{artifact.staging_digest.removeprefix('sha256:')[:24]}",
                    job_id,
                    "semantic dimension published after Admin confirmation" if publication is not None else "semantic dimension staging is ready for Admin publish decision",
                    json.dumps({"staging_digest": artifact.staging_digest, "published_digest": next_published_digest}, sort_keys=True),
                    now.isoformat(),
                ),
            )

    def release(self, *, job_id: str, owner: str) -> None:
        now = _now().isoformat()
        with self._connect() as connection:
            connection.execute(
                "UPDATE knowledge_authoring_jobs SET status = 'queued', current_step = 'queued', progress = 0, retry_count = retry_count + 1, "
                "error_message = 'worker released after failure', lease_owner = NULL, lease_expires_at = NULL, heartbeat_at = NULL, updated_at = ? "
                "WHERE id = ? AND status = 'running' AND lease_owner = ?",
                (now, job_id, owner),
            )
