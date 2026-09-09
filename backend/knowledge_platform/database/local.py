"""Small local/test implementations for Database Query ports."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from knowledge_contracts import QueryPlan

from .ports import (
    DatabaseDatasetBinding,
    DatabaseEvidenceRecord,
    DatabaseExecution,
    DatabaseSchemaEvidence,
    DatabaseSqlCandidate,
    StoredQueryPlan,
)

_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
_TABLE_RE = re.compile(r"^[A-Za-z0-9_$-]{1,128}(?:\.[A-Za-z0-9_$-]{1,128})?$")
_MAX_SOURCE_BYTES = 512 * 1024 * 1024
_SECRET_COLUMN_RE = re.compile(
    r"(?i)(?:password|passwd|secret|token|authorization|api[_ -]?key|private[_ -]?key)"
)


@dataclass(frozen=True, slots=True)
class LocalSqliteDatabaseSource:
    """Host-bound source facts; this object must never cross a public API."""

    dataset_id: str
    space_id: str
    path: Path
    allowed_tables: tuple[str, ...]
    dataset_version: str
    deployment_revision: str
    semantic_context_hash: str
    provider_version: str = "local-sqlite"

    def __post_init__(self) -> None:
        for value, field_name in (
            (self.dataset_id, "dataset_id"),
            (self.space_id, "space_id"),
            (self.dataset_version, "dataset_version"),
            (self.deployment_revision, "deployment_revision"),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must not be empty")
        if not _ID_RE.fullmatch(self.dataset_id) or not _ID_RE.fullmatch(self.space_id):
            raise ValueError("local SQLite source identity is invalid")
        if not isinstance(self.path, Path):
            raise ValueError("local SQLite source path must be a Path")
        if not self.allowed_tables or any(
            not isinstance(table, str) or not _TABLE_RE.fullmatch(table) for table in self.allowed_tables
        ):
            raise ValueError("local SQLite source table allowlist is invalid")
        if len(set(self.allowed_tables)) != len(self.allowed_tables):
            raise ValueError("local SQLite source table allowlist must be unique")


class LocalSqliteDatabaseDatasetResolver:
    """Resolve a current binding from one explicit, host-owned SQLite file.

    Paths are accepted only at application/bootstrap time.  The returned
    ``DatabaseDatasetBinding`` intentionally contains no path or credential;
    every resolve rechecks file identity and the selected schema.
    """

    def __init__(self, sources: Sequence[LocalSqliteDatabaseSource]) -> None:
        self._sources: dict[tuple[str, str], LocalSqliteDatabaseSource] = {}
        for source in sources:
            key = (source.space_id, source.dataset_id)
            if key in self._sources:
                raise ValueError("local SQLite database source binding is duplicated")
            self._sources[key] = source

    def resolve(self, *, dataset_id: str, space_id: str) -> DatabaseDatasetBinding | None:
        source = self._sources.get((space_id, dataset_id))
        if source is None:
            return None
        path = source.path.expanduser().absolute()
        self._assert_safe_file(path)
        source_revision = self._file_digest(path)
        self._assert_schema(path, source.allowed_tables)
        if self._file_digest(path) != source_revision:
            raise LookupError("local database source changed during binding")
        return DatabaseDatasetBinding(
            dataset_id=source.dataset_id,
            space_id=source.space_id,
            dataset_version=source.dataset_version,
            deployment_revision=source.deployment_revision,
            dialect="sqlite",
            allowed_tables=source.allowed_tables,
            semantic_context_hash=source.semantic_context_hash,
            source_revision=source_revision,
            provider_version=source.provider_version,
        )

    @classmethod
    def _assert_safe_file(cls, path: Path) -> None:
        if path.is_symlink() or not path.is_file():
            raise LookupError("local database binding is unavailable")
        # macOS commonly exposes temporary directories through /var ->
        # /private/var.  Resolve that host alias, while still rejecting a
        # symlink at the configured database file itself.
        resolved = path.resolve(strict=True)
        if resolved.is_symlink() or not resolved.is_file():
            raise LookupError("local database binding is unavailable")

    @classmethod
    def _file_digest(cls, path: Path) -> str:
        cls._assert_safe_file(path)
        if path.stat().st_size > _MAX_SOURCE_BYTES:
            raise LookupError("local database source exceeds the local bound")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        return f"sha256:{digest.hexdigest()}"

    @classmethod
    def _assert_schema(cls, path: Path, allowed_tables: Sequence[str]) -> None:
        uri = f"file:{quote(str(path), safe='/')}?mode=ro"
        try:
            connection = sqlite3.connect(uri, uri=True)
        except sqlite3.Error as error:
            raise LookupError("local database could not be opened read-only") from error
        try:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA trusted_schema=OFF")
            actual_tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            actual_by_fold = {name.casefold(): name for name in actual_tables}
            for requested in allowed_tables:
                actual = actual_by_fold.get(requested.casefold())
                if actual is None:
                    raise LookupError("local database table binding is unavailable")
                columns = [
                    str(row[0])
                    for row in connection.execute(
                        "SELECT name FROM pragma_table_info(?)", (actual.split(".")[-1],)
                    )
                ]
                if not columns or any(_SECRET_COLUMN_RE.search(column) for column in columns):
                    raise PermissionError("local database schema contains a secret-bearing column")
        except sqlite3.Error as error:
            raise LookupError("local database schema could not be inspected") from error
        finally:
            connection.close()


class StaticDatabaseDatasetResolver:
    def __init__(self, bindings: Mapping[tuple[str, str], DatabaseDatasetBinding]) -> None:
        self._bindings = dict(bindings)

    def resolve(self, *, dataset_id: str, space_id: str) -> DatabaseDatasetBinding | None:
        binding = self._bindings.get((space_id, dataset_id))
        return binding


class StaticNl2SqlProvider:
    """Deterministic local provider used for contract tests and shadow runs."""

    def __init__(self, candidates: Mapping[str, DatabaseSqlCandidate]) -> None:
        self._candidates = dict(candidates)

    def generate(
        self,
        *,
        question: str,
        binding: DatabaseDatasetBinding,
        semantic_asset_ids: Sequence[str],
    ) -> DatabaseSqlCandidate:
        del binding, semantic_asset_ids
        candidate = self._candidates.get(question)
        if candidate is None:
            raise LookupError("no grounded SQL candidate is available")
        return candidate


class InMemoryQueryPlanRepository:
    def __init__(self) -> None:
        self._items: dict[str, StoredQueryPlan] = {}

    def put(self, *, stored: StoredQueryPlan) -> None:
        plan_id = stored.plan.query_plan_id
        if plan_id in self._items:
            raise ValueError("query plan id already exists")
        self._items[plan_id] = stored

    def get(self, *, query_plan_id: str) -> StoredQueryPlan | None:
        return self._items.get(query_plan_id)


class InMemoryQueryResultRepository:
    def __init__(self) -> None:
        self._items: dict[str, DatabaseExecution] = {}

    def put(self, *, result: DatabaseExecution, space_id: str, owner_subject_id: str) -> str:
        material = {
            "space_id": space_id,
            "owner_subject_id": owner_subject_id,
            "columns": result.columns,
            "rows": result.rows,
            "row_count": result.row_count,
            "issued_at": time.time_ns(),
        }
        result_id = "result_" + hashlib.sha256(
            json.dumps(material, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:40]
        self._items[result_id] = result
        return result_id

    def get(self, result_id: str) -> DatabaseExecution | None:
        return self._items.get(result_id)


class InMemoryDatabaseEvidenceRepository:
    """Local repository implementation for Platform-owned evidence facts."""

    def __init__(self) -> None:
        self._items: dict[str, DatabaseEvidenceRecord] = {}

    def put(self, *, record: DatabaseEvidenceRecord) -> None:
        if record.evidence_id in self._items:
            raise ValueError("database evidence id already exists")
        self._items[record.evidence_id] = record

    def get(
        self,
        *,
        evidence_id: str,
        owner_subject_id: str,
        space_id: str,
        dataset_id: str,
        source_revision: str,
        allowed_tables: Sequence[str],
    ) -> DatabaseEvidenceRecord | None:
        record = self._items.get(evidence_id)
        try:
            expired = datetime.fromisoformat(record.expires_at) <= datetime.now(timezone.utc) if record else True
        except (TypeError, ValueError):
            expired = True
        if record is None or expired or any(
            (
                record.owner_subject_id != owner_subject_id,
                record.space_id != space_id,
                record.dataset_id != dataset_id,
                record.source_revision != source_revision,
                not set(allowed_tables).issubset(set(record.allowed_tables)),
            )
        ):
            return None
        return record


class InMemoryDatabaseSchemaEvidenceRepository:
    def __init__(self) -> None:
        self._items: dict[tuple[str, str, str, str, str], DatabaseSchemaEvidence] = {}

    def put(self, *, record: DatabaseSchemaEvidence) -> None:
        key = (
            record.space_id,
            record.dataset_id,
            record.owner_subject_id,
            record.table_name.casefold(),
            record.schema_revision,
        )
        if key in self._items:
            raise ValueError("database schema evidence already exists")
        self._items[key] = record

    def get(
        self,
        *,
        space_id: str,
        dataset_id: str,
        owner_subject_id: str,
        table_name: str,
        schema_revision: str,
    ) -> DatabaseSchemaEvidence | None:
        return self._items.get((space_id, dataset_id, owner_subject_id, table_name.casefold(), schema_revision))


class StaticReadonlyDatabaseExecutor:
    def __init__(self, results: Mapping[str, DatabaseExecution]) -> None:
        self._results = dict(results)

    def execute(
        self,
        *,
        plan: QueryPlan,
        space_id: str,
        allowed_tables: Sequence[str],
        page_size: int,
    ) -> DatabaseExecution:
        del space_id, allowed_tables
        result = self._results.get(plan.query_plan_id)
        if result is None:
            raise LookupError("no local execution result is available")
        rows = result.rows[:page_size]
        return DatabaseExecution(
            columns=result.columns,
            rows=tuple(rows),
            row_count=result.row_count,
            limited=result.limited or len(rows) < len(result.rows),
            result_id=result.result_id,
        )
