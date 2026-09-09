"""Explicit Space ownership for Platform QueryResult artifacts.

QueryResult rows may be created by different application capabilities.  This
store is the only local binding primitive that makes an artifact eligible for
MCP Resource exposure; an absent binding is never inferred from correlation
or artifact URI fields.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")


class QueryResultScopeReader(Protocol):
    def get_space_id(self, *, query_result_id: str) -> str | None: ...


def _valid_id(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        raise ValueError(f"{label} is invalid")
    return value


class SqliteQueryResultScopeStore:
    """Read/write explicit QueryResult-to-Space bindings in a Platform DB."""

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path.expanduser().absolute()
        if self._database_path.is_symlink() or not self._database_path.is_file():
            raise FileNotFoundError(f"Catalog database does not exist: {self._database_path}")

    @property
    def catalog_revision(self) -> str:
        digest = hashlib.sha256()
        for candidate in (
            self._database_path,
            Path(f"{self._database_path}-wal"),
            Path(f"{self._database_path}-shm"),
        ):
            if candidate.is_symlink():
                raise OSError("Catalog sidecar must not be a symlink")
            if not candidate.is_file():
                continue
            digest.update(candidate.name.encode("utf-8"))
            digest.update(b"\0")
            with candidate.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
        return f"sha256:{digest.hexdigest()}"

    def bind_query_result(
        self, *, query_result_id: str, space_id: str, bound_at: str | None = None
    ) -> dict[str, str]:
        query_result_id = _valid_id(query_result_id, label="query_result_id")
        space_id = _valid_id(space_id, label="space_id")
        timestamp = bound_at or datetime.now(UTC).isoformat()
        if not isinstance(timestamp, str) or not timestamp.strip():
            raise ValueError("QueryResult binding timestamp is invalid")
        try:
            parsed_timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("QueryResult binding timestamp is invalid") from exc
        if parsed_timestamp.tzinfo is None or parsed_timestamp.utcoffset() is None:
            raise ValueError("QueryResult binding timestamp must include timezone")
        timestamp = parsed_timestamp.astimezone(UTC).isoformat()
        with sqlite3.connect(self._database_path) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                "SELECT 1 FROM knowledge_query_results WHERE id = ?", (query_result_id,)
            ).fetchone() is None:
                raise LookupError("QueryResult does not exist")
            if connection.execute(
                "SELECT 1 FROM knowledge_spaces WHERE id = ?", (space_id,)
            ).fetchone() is None:
                raise LookupError("Space does not exist")
            existing = connection.execute(
                "SELECT space_id, bound_at FROM knowledge_query_result_scopes WHERE query_result_id = ?",
                (query_result_id,),
            ).fetchone()
            if existing is not None:
                if str(existing[0]) != space_id:
                    raise ValueError("QueryResult is already bound to another Space")
                return {
                    "query_result_id": query_result_id,
                    "space_id": space_id,
                    "bound_at": str(existing[1]),
                }
            connection.execute(
                "INSERT INTO knowledge_query_result_scopes (query_result_id, space_id, bound_at) VALUES (?, ?, ?)",
                (query_result_id, space_id, timestamp),
            )
        return {"query_result_id": query_result_id, "space_id": space_id, "bound_at": timestamp}

    def get_space_id(self, *, query_result_id: str) -> str | None:
        query_result_id = _valid_id(query_result_id, label="query_result_id")
        revision_before = self.catalog_revision
        uri = f"file:{self._database_path}?mode=ro"
        with sqlite3.connect(uri, uri=True) as connection:
            row = connection.execute(
                "SELECT space_id FROM knowledge_query_result_scopes WHERE query_result_id = ?",
                (query_result_id,),
            ).fetchone()
        if revision_before != self.catalog_revision:
            raise OSError("Catalog changed during QueryResult scope read")
        return str(row[0]) if row is not None else None


__all__ = ["QueryResultScopeReader", "SqliteQueryResultScopeStore"]
