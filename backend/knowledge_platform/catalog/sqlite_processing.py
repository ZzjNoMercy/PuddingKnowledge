"""Fenced SQLite job store for logical dataset Processing."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from knowledge_contracts import is_valid_knowledge_uri
from knowledge_platform.structured.job_worker import LogicalDatasetProcessingJobResult

_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _key_digest(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise ValueError("logical dataset Processing idempotency key is invalid")
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


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


class SqliteLogicalDatasetProcessingJobStore:
    """Persist only digests and portable terminal metadata, never source paths."""

    def __init__(self, *, database_path: Path) -> None:
        self._database_path = database_path.expanduser().absolute()
        if self._database_path.is_symlink() or not self._database_path.is_file():
            raise FileNotFoundError("logical dataset Processing Catalog database is unavailable")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @staticmethod
    def _job_id(key_digest: str) -> str:
        return "logical_process_" + key_digest.removeprefix("sha256:")[:48]

    def claim(
        self, *, dataset_id: str, space_id: str, idempotency_key: str, binding_digest: str
    ) -> tuple[bool, str, str, LogicalDatasetProcessingJobResult | None]:
        if not _ID_RE.fullmatch(dataset_id) or not _ID_RE.fullmatch(space_id) or not _DIGEST_RE.fullmatch(binding_digest):
            raise ValueError("logical dataset Processing binding is invalid")
        key_digest = _key_digest(idempotency_key)
        job_id = self._job_id(key_digest)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT space_id, kind, status, source_sha256, lease_owner, lease_expires_at, metadata_json, attempt "
                "FROM knowledge_processing_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if row is not None:
                if row["kind"] != "logical_dataset_processing" or row["source_sha256"] != binding_digest or row["space_id"] != space_id:
                    raise ValueError("logical dataset Processing idempotency key collision")
                try:
                    metadata = json.loads(row["metadata_json"] or "{}")
                except json.JSONDecodeError as error:
                    raise ValueError("logical dataset Processing metadata is invalid") from error
                if not isinstance(metadata, dict) or metadata.get("dataset_id") != dataset_id:
                    raise ValueError("logical dataset Processing key is bound to another dataset")
                if row["status"] == "succeeded":
                    return False, job_id, "", LogicalDatasetProcessingJobResult(
                        job_id=job_id,
                        dataset_id=dataset_id,
                        space_id=str(row["space_id"]),
                        resource_uri=str(metadata.get("resource_uri") or ""),
                        content_digest=str(metadata.get("content_digest") or ""),
                        row_count=int(metadata.get("row_count") or 0),
                    )
                if row["status"] == "running" and _live(row["lease_expires_at"], _now()):
                    return False, job_id, "", None
                if row["status"] != "running":
                    raise ValueError("stored logical dataset Processing job has an unsupported status")
                now = _now()
                owner = "logical-worker-" + secrets.token_hex(12)
                connection.execute(
                    "UPDATE knowledge_processing_jobs SET lease_owner = ?, lease_expires_at = ?, heartbeat_at = ?, "
                    "started_at = ?, finished_at = NULL, current_step = 'processing', progress = 0, error_message = '', "
                    "attempt = ?, updated_at = ? WHERE id = ? AND status = 'running'",
                    (owner, (now + timedelta(minutes=5)).isoformat(), now.isoformat(), now.isoformat(), int(row["attempt"] or 0) + 1, now.isoformat(), job_id),
                )
                return True, job_id, owner, None
            now = _now()
            owner = "logical-worker-" + secrets.token_hex(12)
            connection.execute(
                """
                INSERT INTO knowledge_processing_jobs
                    (id, space_id, kind, status, title, file_name, file_type, file_size,
                     input_uri, input_reference_digest, source_sha256, current_step, progress,
                     error_message, retry_count, metadata_json, created_at, updated_at,
                     started_at, finished_at, lease_owner, lease_expires_at, heartbeat_at, attempt)
                VALUES (?, ?, 'logical_dataset_processing', 'running', 'Logical Dataset Processing', '', 'structured', 0,
                        '', ?, ?, 'processing', 0, '', 0, ?, ?, ?, ?, NULL, ?, ?, ?, 1)
                """,
                (
                    job_id,
                    space_id,
                    key_digest,
                    binding_digest,
                    json.dumps({"dataset_id": dataset_id, "binding_digest": binding_digest}, sort_keys=True),
                    now.isoformat(),
                    now.isoformat(),
                    now.isoformat(),
                    owner,
                    (now + timedelta(minutes=5)).isoformat(),
                    now.isoformat(),
                ),
            )
            return True, job_id, owner, None

    def complete(
        self,
        *,
        job_id: str,
        owner: str,
        dataset_id: str,
        space_id: str,
        binding_digest: str,
        resource_uri: str,
        content_digest: str,
        row_count: int,
    ) -> LogicalDatasetProcessingJobResult:
        if not _ID_RE.fullmatch(job_id) or not _ID_RE.fullmatch(space_id) or not _DIGEST_RE.fullmatch(binding_digest) or not _DIGEST_RE.fullmatch(content_digest) or type(row_count) is not int or row_count < 0:
            raise ValueError("logical dataset Processing completion is invalid")
        expected_uri = f"knowledge://spaces/{space_id}/structured-assets/{dataset_id}/source"
        if not is_valid_knowledge_uri(resource_uri) or resource_uri != expected_uri:
            raise ValueError("logical dataset Processing resource URI is invalid")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT space_id, kind, status, source_sha256, lease_owner, lease_expires_at, metadata_json "
                "FROM knowledge_processing_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if row is None or row["space_id"] != space_id or row["kind"] != "logical_dataset_processing" or row["status"] != "running" or row["lease_owner"] != owner or not _live(row["lease_expires_at"], _now()) or row["source_sha256"] != binding_digest:
                raise ValueError("logical dataset Processing lease is lost")
            metadata = json.loads(row["metadata_json"] or "{}")
            if not isinstance(metadata, dict) or metadata.get("dataset_id") != dataset_id:
                raise ValueError("logical dataset Processing dataset binding is invalid")
            now = _now()
            metadata.update({"resource_uri": resource_uri, "content_digest": content_digest, "row_count": row_count})
            cursor = connection.execute(
                "UPDATE knowledge_processing_jobs SET space_id = ?, status = 'succeeded', current_step = 'completed', "
                "progress = 100, input_uri = ?, metadata_json = ?, finished_at = ?, updated_at = ?, lease_owner = NULL, "
                "lease_expires_at = NULL, heartbeat_at = NULL WHERE id = ? AND status = 'running' AND lease_owner = ?",
                (space_id, resource_uri, json.dumps(metadata, sort_keys=True), now.isoformat(), now.isoformat(), job_id, owner),
            )
            if cursor.rowcount != 1:
                raise ValueError("logical dataset Processing completion fence failed")
        return LogicalDatasetProcessingJobResult(job_id, dataset_id, space_id, resource_uri, content_digest, row_count)

    def release(self, *, job_id: str, owner: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM knowledge_processing_jobs WHERE id = ? AND status = 'running' AND lease_owner = ?",
                (job_id, owner),
            )
