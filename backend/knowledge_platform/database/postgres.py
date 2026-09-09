"""Host-bound PostgreSQL adapters for local, read-only Database shadows."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

import sqlglot
from sqlglot import exp

from knowledge_contracts import QueryPlan, QueryPlanValidation

from .ports import DatabaseDatasetBinding, DatabaseExecution, DatabaseSchemaTable

_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_$-]{1,128}$")
_SECRET_COLUMN_RE = re.compile(
    r"(?i)(?:password|passwd|secret|token|authorization|api[_ -]?key|private[_ -]?key)"
)
_DANGEROUS_RE = re.compile(
    r"\b(insert|update|delete|drop|alter|truncate|create|grant|revoke|copy|vacuum|reindex|refresh|comment)\b",
    re.IGNORECASE,
)
_DANGEROUS_FUNCTION_RE = re.compile(
    r"\b(?:pg_read_file|pg_read_binary_file|pg_ls_dir|pg_stat_file|pg_sleep|dblink|lo_import|lo_export|set_config)\s*\(",
    re.IGNORECASE,
)
_MAX_REVISION_ROWS = 100_000
_MAX_ROWS = 500


@dataclass(frozen=True, slots=True)
class LocalPostgresDatabaseSource:
    """Host-only connection facts; never serialize or expose this object."""

    dataset_id: str
    space_id: str
    host: str
    port: int
    database: str
    username: str
    allowed_tables: tuple[str, ...]
    dataset_version: str
    deployment_revision: str
    semantic_context_hash: str
    password: str = ""
    provider_version: str = "local-postgresql"

    def __post_init__(self) -> None:
        for value, field_name in (
            (self.dataset_id, "dataset_id"),
            (self.space_id, "space_id"),
            (self.host, "host"),
            (self.database, "database"),
            (self.username, "username"),
            (self.dataset_version, "dataset_version"),
            (self.deployment_revision, "deployment_revision"),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must not be empty")
        if not _ID_RE.fullmatch(self.dataset_id) or not _ID_RE.fullmatch(self.space_id):
            raise ValueError("local PostgreSQL source identity is invalid")
        if self.host.casefold() not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("local PostgreSQL source must use a loopback host")
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("local PostgreSQL source port is invalid")
        if not self.allowed_tables or any(not _valid_table_name(table) for table in self.allowed_tables):
            raise ValueError("local PostgreSQL source table allowlist is invalid")
        if len(set(self.allowed_tables)) != len(self.allowed_tables):
            raise ValueError("local PostgreSQL source table allowlist must be unique")


def _valid_table_name(value: str) -> bool:
    parts = str(value).split(".")
    return len(parts) in {1, 2} and all(_IDENTIFIER_RE.fullmatch(part) for part in parts)


def _table_parts(value: str) -> tuple[str, str]:
    parts = str(value).split(".")
    return (parts[0], parts[1]) if len(parts) == 2 else ("public", parts[0])


def _quote_identifier(value: str) -> str:
    if not _IDENTIFIER_RE.fullmatch(value):
        raise ValueError("PostgreSQL identifier is invalid")
    return '"' + value.replace('"', '""') + '"'


def _run_async(factory: Callable[[], Any]) -> Any:
    """Run an asyncpg operation from both sync callers and an active event loop."""

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(factory())

    result: list[Any] = []
    failure: list[BaseException] = []

    def worker() -> None:
        try:
            result.append(asyncio.run(factory()))
        except BaseException as error:  # propagate the original database failure
            failure.append(error)

    thread = threading.Thread(target=worker, name="platform-postgres-shadow", daemon=True)
    thread.start()
    thread.join()
    if failure:
        raise failure[0]
    return result[0] if result else None


async def _connect(source: LocalPostgresDatabaseSource):
    try:
        import asyncpg
    except ImportError as error:  # pragma: no cover - depends on optional install
        raise RuntimeError("asyncpg is required for the local PostgreSQL binding") from error
    return await asyncpg.connect(
        host=source.host,
        port=source.port,
        database=source.database,
        user=source.username,
        password=source.password,
        timeout=5,
    )


async def _schema_and_revision(source: LocalPostgresDatabaseSource) -> str:
    connection = await _connect(source)
    try:
        digest = hashlib.sha256()
        async with connection.transaction(isolation="repeatable_read", readonly=True):
            for requested in source.allowed_tables:
                schema, table = _table_parts(requested)
                base_table = await connection.fetchval(
                    """
                    SELECT 1
                    FROM information_schema.tables
                    WHERE table_schema = $1 AND table_name = $2 AND table_type = 'BASE TABLE'
                    """,
                    schema,
                    table,
                )
                if base_table != 1:
                    raise LookupError("local PostgreSQL table binding is unavailable")
                columns = await connection.fetch(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema = $1 AND table_name = $2
                    ORDER BY ordinal_position
                    """,
                    schema,
                    table,
                )
                names = tuple(str(row["column_name"]) for row in columns)
                if not names:
                    raise LookupError("local PostgreSQL table binding is unavailable")
                if any(_SECRET_COLUMN_RE.search(name) for name in names):
                    raise PermissionError("local PostgreSQL schema contains a secret-bearing column")
                digest.update(json.dumps({"table": f"{schema}.{table}", "columns": names}, sort_keys=True).encode())
                quoted_columns = ", ".join(_quote_identifier(name) for name in names)
                qualified = f"{_quote_identifier(schema)}.{_quote_identifier(table)}"
                order = ", ".join(f"{_quote_identifier(name)} ASC NULLS FIRST" for name in names)
                rows = await connection.fetch(
                    f"SELECT {quoted_columns} FROM {qualified} ORDER BY {order} LIMIT $1",
                    _MAX_REVISION_ROWS + 1,
                )
                if len(rows) > _MAX_REVISION_ROWS:
                    raise LookupError("local PostgreSQL source exceeds the revision bound")
                for row in rows:
                    digest.update(json.dumps([_portable(value) for value in row], ensure_ascii=False, default=str).encode())
        return f"sha256:{digest.hexdigest()}"
    finally:
        await connection.close()


async def _schema_tables(source: LocalPostgresDatabaseSource) -> tuple[DatabaseSchemaTable, ...]:
    connection = await _connect(source)
    try:
        tables: list[DatabaseSchemaTable] = []
        async with connection.transaction(isolation="repeatable_read", readonly=True):
            for requested in source.allowed_tables:
                schema, table = _table_parts(requested)
                base_table = await connection.fetchval(
                    """
                    SELECT 1
                    FROM information_schema.tables
                    WHERE table_schema = $1 AND table_name = $2 AND table_type = 'BASE TABLE'
                    """,
                    schema,
                    table,
                )
                if base_table != 1:
                    raise LookupError("local PostgreSQL table binding is unavailable")
                columns = await connection.fetch(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema = $1 AND table_name = $2
                    ORDER BY ordinal_position
                    """,
                    schema,
                    table,
                )
                names = tuple(str(row["column_name"]) for row in columns)
                if not names or any(_SECRET_COLUMN_RE.search(name) for name in names):
                    raise PermissionError("local PostgreSQL schema contains a secret-bearing column")
                schema_revision = "sha256:" + hashlib.sha256(
                    json.dumps({"table": f"{schema}.{table}", "columns": names}, sort_keys=True).encode()
                ).hexdigest()
                tables.append(
                    DatabaseSchemaTable(
                        table_name=f"{schema}.{table}",
                        columns=names,
                        schema_revision=schema_revision,
                    )
                )
        return tuple(tables)
    finally:
        await connection.close()


class LocalPostgresDatabaseSchemaReader:
    """Read only the tables already bound by the host-owned source."""

    def __init__(self, sources: Mapping[tuple[str, str], LocalPostgresDatabaseSource]) -> None:
        self._sources = dict(sources)

    def read(self, *, binding: DatabaseDatasetBinding) -> tuple[DatabaseSchemaTable, ...]:
        source = self._sources.get((binding.space_id, binding.dataset_id))
        if source is None:
            raise LookupError("local PostgreSQL schema binding is unavailable")
        return _run_async(lambda: _schema_tables(source))


class LocalPostgresDatabaseDatasetResolver:
    """Resolve current PostgreSQL schema/data facts without exposing credentials."""

    def __init__(self, sources: Sequence[LocalPostgresDatabaseSource]) -> None:
        self._sources: dict[tuple[str, str], LocalPostgresDatabaseSource] = {}
        for source in sources:
            key = (source.space_id, source.dataset_id)
            if key in self._sources:
                raise ValueError("local PostgreSQL database source binding is duplicated")
            self._sources[key] = source

    def resolve(self, *, dataset_id: str, space_id: str) -> DatabaseDatasetBinding | None:
        source = self._sources.get((space_id, dataset_id))
        if source is None:
            return None
        revision = _run_async(lambda: _schema_and_revision(source))
        return DatabaseDatasetBinding(
            dataset_id=source.dataset_id,
            space_id=source.space_id,
            dataset_version=source.dataset_version,
            deployment_revision=source.deployment_revision,
            dialect="postgresql",
            allowed_tables=source.allowed_tables,
            semantic_context_hash=source.semantic_context_hash,
            source_revision=revision,
            provider_version=source.provider_version,
        )


class PostgresReadonlySqlValidator:
    """Validate one PostgreSQL SELECT/WITH statement against an allowlist."""

    def validate(
        self,
        *,
        sql: str,
        dialect: str,
        allowed_tables: Sequence[str],
    ) -> QueryPlanValidation:
        clean = sql.strip()
        readonly = (
            dialect.casefold() in {"postgres", "postgresql"}
            and bool(re.match(r"^(?:select|with)\b", clean, re.IGNORECASE))
            and ";" not in clean
            and not _DANGEROUS_RE.search(clean)
            and not _DANGEROUS_FUNCTION_RE.search(clean)
        )
        if not readonly:
            return QueryPlanValidation(readonly=False, allowed_tables=False, guardrails_passed=False)
        try:
            tree = sqlglot.parse_one(clean, dialect="postgres")
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
            allowed_names = {".".join(part.casefold() for part in _table_parts(table)) for table in allowed_tables}
            allowed = bool(actual_tables) and actual_tables.issubset(allowed_names | {name.split(".")[-1] for name in allowed_names if name.startswith("public.")})
        except (sqlglot.errors.ParseError, ValueError):
            allowed = False
        return QueryPlanValidation(readonly=True, allowed_tables=allowed, guardrails_passed=allowed)


def _portable(value: object) -> object:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("PostgreSQL result contains a non-finite float")
        return value
    if isinstance(value, (Decimal, date, datetime, time)):
        return str(value)
    raise ValueError("PostgreSQL result contains a non-portable value")


async def _execute_postgres(
    source: LocalPostgresDatabaseSource,
    *,
    sql: str,
    page_size: int,
) -> DatabaseExecution:
    connection = await _connect(source)
    try:
        async with connection.transaction(isolation="repeatable_read", readonly=True):
            schemas = sorted({_table_parts(table)[0] for table in source.allowed_tables})
            search_path = ", ".join(
                [_quote_identifier(schema) for schema in schemas] + [_quote_identifier("pg_catalog")]
            )
            await connection.execute(f"SET LOCAL search_path = {search_path}")
            await connection.execute("SET LOCAL statement_timeout = '15s'")
            statement = await connection.prepare(f"SELECT * FROM ({sql}) AS platform_result LIMIT $1")
            attributes = tuple(str(attribute.name) for attribute in statement.get_attributes())
            raw_rows = await statement.fetch(page_size + 1)
            limited = len(raw_rows) > page_size
            rows = tuple(
                {column: _portable(row[index]) for index, column in enumerate(attributes)}
                for row in raw_rows[:page_size]
            )
            row_count = len(rows)
            if limited:
                row_count = int(
                    await connection.fetchval(f"SELECT COUNT(*) FROM ({sql}) AS platform_count")
                )
            return DatabaseExecution(
                columns=attributes,
                rows=rows,
                row_count=row_count,
                limited=limited,
            )
    finally:
        await connection.close()


class PostgresReadonlyDatabaseExecutor:
    """Run one approved plan through a transaction-level PostgreSQL read fence."""

    def __init__(
        self,
        sources: Mapping[tuple[str, str], LocalPostgresDatabaseSource],
        *,
        validator: PostgresReadonlySqlValidator | None = None,
        source_revisions: Mapping[tuple[str, str], str] | None = None,
    ) -> None:
        self._sources = dict(sources)
        self._validator = validator or PostgresReadonlySqlValidator()
        self._source_revisions = dict(source_revisions or {})

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
            raise PermissionError("PostgreSQL query plan did not pass read-only validation")
        source = self._sources.get((space_id, plan.dataset_id))
        if source is None:
            raise LookupError("local PostgreSQL database binding is unavailable")
        expected = self._source_revisions.get((space_id, plan.dataset_id))
        result = _run_async(lambda: _execute_postgres(source, sql=plan.sql, page_size=page_size))
        if expected is not None and _run_async(lambda: _schema_and_revision(source)) != expected:
            raise LookupError("local PostgreSQL source changed during execution")
        return result
