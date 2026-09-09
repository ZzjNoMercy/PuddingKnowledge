"""SQLite-backed Wiki compilation idempotency store.

The store reuses the Platform-owned ``knowledge_processing_jobs`` table.  It
persists only a digest of the idempotency key and the portable terminal Wiki
URI; raw keys, local paths, source text, and Harness identities never enter
the Catalog.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from knowledge_contracts import is_valid_knowledge_uri

from .ports import WikiCompilationClaim

_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _key_digest(idempotency_key: str) -> str:
    if not isinstance(idempotency_key, str) or not idempotency_key.strip() or len(idempotency_key) > 512:
        raise ValueError("Wiki idempotency key is invalid")
    return "sha256:" + hashlib.sha256(idempotency_key.encode()).hexdigest()


def _job_id(key_digest: str) -> str:
    return "wiki_compile_" + key_digest.removeprefix("sha256:")[:48]


class SqliteWikiCompilationJobStore:
    """Cross-process claim store for one explicit Platform Catalog database."""

    def __init__(self, *, database_path: Path, space_id: str) -> None:
        self._database_path = database_path.expanduser().absolute()
        if self._database_path.is_symlink() or not self._database_path.is_file():
            raise FileNotFoundError("Wiki compilation Catalog database is unavailable")
        if not _ID_RE.fullmatch(space_id):
            raise ValueError("Wiki compilation Space is invalid")
        self._space_id = space_id

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    async def claim(self, *, idempotency_key: str) -> WikiCompilationClaim:
        key_digest = _key_digest(idempotency_key)
        job_id = _job_id(key_digest)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT space_id, status, input_uri, input_reference_digest FROM knowledge_processing_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if row is not None:
                if str(row["input_reference_digest"] or "") != key_digest:
                    raise ValueError("Wiki compilation idempotency digest collision")
                if str(row["space_id"] or "") != self._space_id:
                    raise ValueError("Wiki compilation idempotency key is bound to another Space")
                status = str(row["status"] or "")
                if status == "succeeded":
                    resource_uri = str(row["input_uri"] or "")
                    if not is_valid_knowledge_uri(resource_uri):
                        raise ValueError("stored Wiki resource URI is invalid")
                    return WikiCompilationClaim(False, resource_uri)
                if status == "running":
                    return WikiCompilationClaim(False)
                raise ValueError("stored Wiki compilation job has an unsupported status")
            moment = _now()
            connection.execute(
                """
                INSERT INTO knowledge_processing_jobs
                    (id, space_id, kind, status, title, file_name, file_type,
                     file_size, input_uri, input_reference_digest, source_sha256,
                     current_step, progress, error_message, retry_count,
                     metadata_json, created_at, updated_at, attempt)
                VALUES (?, ?, 'wiki_compile', 'running', 'Wiki compilation',
                        '', 'wiki', 0, '', ?, ?, 'compiling', 0, '', 0,
                        ?, ?, ?, 1)
                """,
                (
                    job_id,
                    self._space_id,
                    key_digest,
                    key_digest,
                    json.dumps({"idempotency_key_digest": key_digest}, sort_keys=True),
                    moment,
                    moment,
                ),
            )
            return WikiCompilationClaim(True)

    async def complete(self, *, idempotency_key: str, resource_uri: str) -> None:
        key_digest = _key_digest(idempotency_key)
        if not is_valid_knowledge_uri(resource_uri):
            raise ValueError("Wiki resource URI is invalid")
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE knowledge_processing_jobs
                   SET status = 'succeeded', current_step = 'published',
                       progress = 100, input_uri = ?, updated_at = ?, finished_at = ?
                 WHERE id = ? AND space_id = ? AND status = 'running'
                       AND input_reference_digest = ?
                """,
                (resource_uri, _now(), _now(), _job_id(key_digest), self._space_id, key_digest),
            )
            if cursor.rowcount != 1:
                raise ValueError("Wiki compilation claim is not owned or no longer running")

    async def release(self, *, idempotency_key: str) -> None:
        key_digest = _key_digest(idempotency_key)
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM knowledge_processing_jobs WHERE id = ? AND space_id = ? AND status = 'running' AND input_reference_digest = ?",
                (_job_id(key_digest), self._space_id, key_digest),
            )
