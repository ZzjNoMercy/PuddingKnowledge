"""Explicit local SQLite read-only executor for Database Query shadow runs."""

from __future__ import annotations

import hashlib
import re
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from urllib.parse import quote

import sqlglot
from sqlglot import exp

from knowledge_contracts import QueryPlan, QueryPlanValidation

from .ports import DatabaseExecution

_DANGEROUS_RE = re.compile(
    r"\b(insert|update|delete|drop|alter|truncate|create|grant|revoke|attach|detach|pragma|vacuum|reindex|replace)\b",
    re.IGNORECASE,
)
_MAX_STEPS = 5_000_000
_MAX_SOURCE_BYTES = 512 * 1024 * 1024
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class SqliteReadonlySqlValidator:
    """Small dependency-local validator; it does not import legacy SQL tools."""

    def __init__(self, guardrail: Callable[[str], bool] | None = None) -> None:
        self._guardrail = guardrail

    def validate(
        self,
        *,
        sql: str,
        dialect: str,
        allowed_tables: Sequence[str],
    ) -> QueryPlanValidation:
        del dialect
        clean = sql.strip()
        readonly = bool(re.match(r"^(?:select|with)\b", clean, re.IGNORECASE)) and ";" not in clean and not _DANGEROUS_RE.search(clean)
        allowed = False
        guardrails = False
        if readonly:
            try:
                tree = sqlglot.parse_one(clean, dialect="sqlite")
                cte_names = {str(item.alias_or_name).casefold() for item in tree.find_all(exp.CTE)}
                actual_tables = set()
                for table in tree.find_all(exp.Table):
                    name = ".".join(
                        str(value).casefold()
                        for value in (table.catalog, table.db, table.name)
                        if value
                    )
                    if name and name not in cte_names:
                        actual_tables.add(name)
                allowed_names = {
                    ".".join(str(part).strip('"').casefold() for part in table.split(".") if str(part).strip())
                    for table in allowed_tables
                }
                allowed = bool(actual_tables) and actual_tables.issubset(allowed_names)
                guardrails = self._guardrail(clean) if self._guardrail is not None else True
            except (sqlglot.errors.ParseError, ValueError):
                pass
        return QueryPlanValidation(readonly=readonly, allowed_tables=allowed, guardrails_passed=guardrails)


class SqliteReadonlyDatabaseExecutor:
    """Execute only a plan against a host-owned local SQLite file binding."""

    def __init__(
        self,
        paths: Mapping[tuple[str, str], Path],
        *,
        validator: SqliteReadonlySqlValidator | None = None,
        source_revisions: Mapping[tuple[str, str], str] | None = None,
        max_steps: int = _MAX_STEPS,
    ) -> None:
        self._paths = {(str(space_id), str(dataset_id)): Path(path) for (space_id, dataset_id), path in paths.items()}
        self._source_revisions = {(str(space_id), str(dataset_id)): str(revision) for (space_id, dataset_id), revision in (source_revisions or {}).items()}
        if any(not _DIGEST_RE.fullmatch(revision) for revision in self._source_revisions.values()):
            raise ValueError("source_revisions must be SHA-256 digests")
        if not set(self._source_revisions).issubset(set(self._paths)):
            raise ValueError("source_revisions contains an unknown database binding")
        self._validator = validator or SqliteReadonlySqlValidator()
        self._max_steps = max(1, min(int(max_steps), _MAX_STEPS))

    def execute(
        self,
        *,
        plan: QueryPlan,
        space_id: str,
        allowed_tables: Sequence[str],
        page_size: int,
    ) -> DatabaseExecution:
        validation = self._validator.validate(
            sql=plan.sql, dialect=plan.dialect, allowed_tables=allowed_tables
        )
        if not validation.passed:
            raise PermissionError("SQLite query plan did not pass read-only validation")
        path = self._paths.get((space_id, plan.dataset_id))
        if path is None or path.is_symlink() or not path.is_file():
            raise LookupError("local database binding is unavailable")
        binding_key = (space_id, plan.dataset_id)
        expected_source_revision = self._source_revisions.get(binding_key)
        if expected_source_revision is not None and self.file_digest(path) != expected_source_revision:
            raise LookupError("local database source revision is stale")
        uri = f"file:{quote(str(path), safe='/')}?mode=ro"
        try:
            connection = sqlite3.connect(uri, uri=True)
        except sqlite3.Error as error:
            raise RuntimeError("local database could not be opened read-only") from error
        try:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA trusted_schema=OFF")
            connection.set_progress_handler(lambda: 1, self._max_steps)
            cursor = connection.execute(
                f"SELECT * FROM ({plan.sql}) AS platform_result LIMIT ?", (page_size + 1,)
            )
            raw_rows = cursor.fetchmany(page_size + 1)
            columns = tuple(str(item[0]) for item in cursor.description or ())
            limited = len(raw_rows) > page_size
            rows = tuple({column: row[index] for index, column in enumerate(columns)} for row in raw_rows[:page_size])
            row_count = len(rows)
            if limited:
                row_count = int(
                    connection.execute(f"SELECT COUNT(*) FROM ({plan.sql}) AS platform_count").fetchone()[0]
                )
            if expected_source_revision is not None and self.file_digest(path) != expected_source_revision:
                raise LookupError("local database source changed during execution")
            return DatabaseExecution(columns=columns, rows=rows, row_count=row_count, limited=limited)
        except sqlite3.Error as error:
            raise RuntimeError("local database read-only execution failed") from error
        finally:
            connection.close()

    @staticmethod
    def file_digest(path: Path) -> str:
        """Return the digest of one explicit regular database file."""

        if path.is_symlink() or not path.is_file():
            raise LookupError("local database binding is unavailable")
        if path.stat().st_size > _MAX_SOURCE_BYTES:
            raise LookupError("local database source exceeds the local bound")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        return f"sha256:{digest.hexdigest()}"
