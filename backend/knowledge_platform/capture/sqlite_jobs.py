"""SQLite-backed idempotency store for Capture Processing jobs."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from knowledge_contracts import is_valid_knowledge_uri
from knowledge_platform.wiki.ports import WikiCompilationClaim

_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")


def _digest(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise ValueError("Capture idempotency key is invalid")
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


class SqliteCaptureProcessingJobStore:
    """Persist only idempotency digest and portable terminal Capture URI."""

    def __init__(self, *, database_path: Path, space_id: str) -> None:
        self._database_path = database_path.expanduser().absolute()
        if self._database_path.is_symlink() or not self._database_path.is_file():
            raise FileNotFoundError("Capture Processing Catalog database is unavailable")
        if not _ID_RE.fullmatch(space_id):
            raise ValueError("Capture Processing Space is invalid")
        self._space_id = space_id

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    async def claim(self, *, idempotency_key: str) -> WikiCompilationClaim:
        key_digest = _digest(idempotency_key)
        job_id = "capture_process_" + key_digest.removeprefix("sha256:")[:48]
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT space_id, status, input_uri, input_reference_digest FROM knowledge_processing_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if row is not None:
                if row["input_reference_digest"] != key_digest or row["space_id"] != self._space_id:
                    raise ValueError("Capture Processing idempotency key is bound to another Space or digest")
                if row["status"] == "succeeded":
                    uri = str(row["input_uri"] or "")
                    if not is_valid_knowledge_uri(uri):
                        raise ValueError("stored Capture resource URI is invalid")
                    return WikiCompilationClaim(False, uri)
                if row["status"] == "running":
                    return WikiCompilationClaim(False)
                raise ValueError("stored Capture Processing job has an unsupported status")
            now = datetime.now(timezone.utc).isoformat()
            connection.execute(
                """
                INSERT INTO knowledge_processing_jobs
                    (id, space_id, kind, status, title, file_name, file_type, file_size,
                     input_uri, input_reference_digest, source_sha256, current_step, progress,
                     error_message, retry_count, metadata_json, created_at, updated_at, attempt)
                VALUES (?, ?, 'read_later_capture', 'running', 'Read Later capture', '', 'markdown', 0,
                        '', ?, ?, 'capturing', 0, '', 0, ?, ?, ?, 1)
                """,
                (job_id, self._space_id, key_digest, key_digest, json.dumps({"key_digest": key_digest}), now, now),
            )
            return WikiCompilationClaim(True)

    async def complete(self, *, idempotency_key: str, resource_uri: str) -> None:
        key_digest = _digest(idempotency_key)
        if not is_valid_knowledge_uri(resource_uri):
            raise ValueError("Capture resource URI is invalid")
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE knowledge_processing_jobs
                   SET status = 'succeeded', current_step = 'published', progress = 100,
                       input_uri = ?, updated_at = ?, finished_at = ?
                 WHERE id = ? AND space_id = ? AND status = 'running' AND input_reference_digest = ?
                """,
                (
                    resource_uri,
                    datetime.now(timezone.utc).isoformat(),
                    datetime.now(timezone.utc).isoformat(),
                    "capture_process_" + key_digest.removeprefix("sha256:")[:48],
                    self._space_id,
                    key_digest,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("Capture Processing claim is not owned or no longer running")

    async def release(self, *, idempotency_key: str) -> None:
        key_digest = _digest(idempotency_key)
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM knowledge_processing_jobs WHERE id = ? AND space_id = ? AND status = 'running' AND input_reference_digest = ?",
                ("capture_process_" + key_digest.removeprefix("sha256:")[:48], self._space_id, key_digest),
            )
