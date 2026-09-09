"""Replay the legacy NL2SQL generation and read-only execution path.

The legacy production source contract supports PostgreSQL/MySQL only. This
observer uses a temporary SQLite engine behind a narrow test-only adapter so
the real service, sqlglot validator, and result contract can be replayed
without requiring a live credentialed database. It does not add SQLite to the
production source registry.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from analytics.nl2sql import service as nl2sql_service
from analytics.nl2sql import sql_runner
from analytics.nl2sql.schemas import DatabaseQueryRequest, TableCandidate, TableRoute
from knowledge import database_sources as database_sources_module
from knowledge_platform.baseline import normalized_digest

FIXTURE_PATH = Path(__file__).resolve().parents[2] / "docs/knowledge-platform/golden-fixtures/database_nl2sql_and_readonly_execute.json"


def _fixture() -> dict[str, Any]:
    document = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    if document.get("format") != "agent-knowledge-platform-golden-fixture/database-nl2sql-readonly/v1":
        raise ValueError("database NL2SQL fixture format is invalid")
    if document.get("sanitized") is not True or not isinstance(document.get("scenario"), dict):
        raise ValueError("database NL2SQL fixture must be explicitly sanitized")
    scenario = document["scenario"]
    if scenario.get("table_name") != "sales_facts" or not isinstance(scenario.get("rows"), list):
        raise ValueError("database NL2SQL fixture table/rows are invalid")
    if any(set(row) != {"brand", "sales"} for row in scenario["rows"]):
        raise ValueError("database NL2SQL fixture rows contain unexpected fields")
    return document


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _source_snapshot(path: Path) -> dict[str, Any]:
    with sqlite3.connect(path) as connection:
        rows = [
            {"brand": row[0], "sales": row[1]}
            for row in connection.execute(
                "SELECT brand, sales FROM sales_facts ORDER BY rowid"
            ).fetchall()
        ]
    return {"table": "sales_facts", "rows": len(rows), "content_digest": normalized_digest(rows)}


def _file_snapshot(root: Path) -> list[dict[str, Any]]:
    return [
        {
            "path": path.relative_to(root).as_posix(),
            "bytes": path.stat().st_size,
            "content_digest": _digest(path.read_bytes()),
        }
        for path in sorted(root.rglob("*"))
        if path.is_file()
    ]


def _source_registry_boundary() -> dict[str, Any]:
    supported_types = sorted(str(item) for item in database_sources_module._SUPPORTED_TYPES)
    try:
        database_sources_module._sanitize_payload(
            {"id": "dbs_sqlite_rejected", "source_type": "sqlite", "database": "fixture"}
        )
    except database_sources_module.KnowledgeDatabaseSourceError as exc:
        sqlite_rejected = str(exc)
    else:
        raise AssertionError("production source registry unexpectedly accepted sqlite")
    return {
        "supported_types": supported_types,
        "sqlite_rejected": sqlite_rejected,
        "unchanged": supported_types == ["mysql", "postgresql"],
    }


class _ConnectionProxy:
    """Ignore PostgreSQL session controls while preserving real SQL execution."""

    def __init__(self, connection: AsyncConnection) -> None:
        self._connection = connection

    async def execute(self, statement: Any, parameters: dict[str, Any] | None = None) -> Any:
        statement_text = str(statement).strip().upper()
        if statement_text.startswith("SET TRANSACTION") or statement_text.startswith("SET LOCAL"):
            return await self._connection.execute(text("SELECT 1"))
        return await self._connection.execute(statement, parameters or {})

    def begin(self) -> _TransactionProxy:
        return _TransactionProxy(self, self._connection)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


class _ConnectionContext:
    def __init__(self, connection: Any) -> None:
        self._connection = connection

    async def __aenter__(self) -> _ConnectionProxy:
        return _ConnectionProxy(await self._connection.__aenter__())

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> Any:
        return await self._connection.__aexit__(exc_type, exc, tb)


class _TransactionProxy:
    def __init__(self, proxy: _ConnectionProxy, connection: AsyncConnection) -> None:
        self._proxy = proxy
        self._transaction = connection.begin()

    async def __aenter__(self) -> _ConnectionProxy:
        await self._transaction.__aenter__()
        return self._proxy

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> Any:
        return await self._transaction.__aexit__(exc_type, exc, tb)


class _EngineProxy:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    def connect(self) -> _ConnectionContext:
        return _ConnectionContext(self._engine.connect())

    async def dispose(self) -> None:
        await self._engine.dispose()


class _FixedGoldenVanna:
    """Fix only the provider boundary; retrieval/refinement remain real calls."""

    def get_related_ddl(self, _question: str) -> list[str]:
        return ["CREATE TABLE sales_facts (brand TEXT, sales INTEGER)"]

    def get_related_documentation(self, _question: str) -> list[str]:
        return ["sales_facts.sales is the integer sales measure; brand is the grouping dimension."]

    def get_similar_question_sql(self, _question: str) -> list[dict[str, str]]:
        return [{"question": "品牌销量汇总", "sql": "SELECT brand, SUM(sales) FROM sales_facts GROUP BY brand"}]

    def generate_sql(self, **_kwargs: Any) -> str:
        return "SELECT brand, SUM(sales) AS total_sales FROM sales_facts GROUP BY brand ORDER BY total_sales DESC"

    def submit_prompt(self, _prompt: list[dict[str, str]]) -> str:
        return "SELECT brand, SUM(sales) AS total_sales FROM sales_facts GROUP BY brand ORDER BY total_sales DESC"


def _route(scenario: dict[str, Any]) -> TableRoute:
    table_name = str(scenario["table_name"])
    return TableRoute(
        database_source_id=str(scenario["database_source_id"]),
        source_name=str(scenario["source_name"]),
        database=str(scenario["database_name"]),
        dialect="PostgreSQL",
        table_names=[table_name],
        available_tables=[table_name],
        candidates=[TableCandidate(name=table_name, columns=["brand", "sales"], score=1.0, reasons=["fixture_scope"])],
        confidence=1.0,
        reason="fixed sanitized fixture scope",
        prompt_context=f"Only table {table_name} is authorized; columns are brand and sales.",
    )


def _stable_result(result: Any) -> dict[str, Any]:
    semantic_assets = result.semantic_assets or {}
    return {
        "question": result.question,
        "sql": result.sql,
        "source": result.source,
        "route": {
            "database_source_id": result.route.database_source_id,
            "table_names": result.route.table_names,
            "dialect": result.route.dialect,
        },
        "execution": {
            "columns": result.execution.columns,
            "rows": result.execution.rows,
            "row_count": result.execution.row_count,
            "limited": result.execution.limited,
            "total_row_count": result.execution.total_row_count,
            "preview_count": result.execution.preview_count,
            "omitted_count": result.execution.omitted_count,
            "is_complete": result.execution.is_complete,
            "profile": result.execution.profile,
        },
        "references": result.references,
        "semantic_assets": {
            "matched": [
                {
                    "id": item.get("id"),
                    "name": item.get("name"),
                    "type": item.get("type"),
                    "match_score": item.get("match_score"),
                }
                for item in semantic_assets.get("matched", [])
                if isinstance(item, dict)
            ],
            "matched_count": semantic_assets.get("matched_count"),
            "resolution_mode": semantic_assets.get("resolution_mode"),
            "semantic_context_hash": semantic_assets.get("semantic_context_hash"),
            "binding_hash": semantic_assets.get("binding_hash"),
        },
        "generation": result.generation,
    }


def _implementation_dependencies() -> list[dict[str, str]]:
    repo_root = Path(__file__).resolve().parents[2]
    relative_paths = (
        "backend/scripts/phase0a_database_nl2sql_observer.py",
        "backend/analytics/nl2sql/service.py",
        "backend/analytics/nl2sql/sql_runner.py",
        "backend/analytics/nl2sql/schemas.py",
        "backend/knowledge/database_sources.py",
    )
    return [{"path": path, "content_digest": _digest((repo_root / path).read_bytes())} for path in relative_paths]


def _validation_case(sql: str, allowed_tables: list[str]) -> dict[str, str]:
    try:
        sql_runner.validate_readonly_sql(sql, allowed_tables=allowed_tables)
    except sql_runner.SqlRunnerError as exc:
        return {"status": "rejected", "error": str(exc)}
    raise AssertionError(f"unsafe SQL was accepted: {sql}")


async def _observe_async(fixture: dict[str, Any]) -> dict[str, Any]:
    scenario = fixture["scenario"]
    with tempfile.TemporaryDirectory(prefix="puddingclaw-golden-database-") as directory:
        root = Path(directory)
        database_path = root / "analytics.sqlite3"
        with sqlite3.connect(database_path) as connection:
            connection.execute("CREATE TABLE sales_facts (brand TEXT NOT NULL, sales INTEGER NOT NULL)")
            connection.executemany(
                "INSERT INTO sales_facts (brand, sales) VALUES (?, ?)",
                [(row["brand"], row["sales"]) for row in scenario["rows"]],
            )
            connection.commit()

        source = {
            "id": scenario["database_source_id"],
            "source_type": "postgresql",
            "name": scenario["source_name"],
            "database": scenario["database_name"],
            "host": "127.0.0.1",
            "port": 5432,
            "username": "fixture",
            "password": "",
        }
        route = _route(scenario)
        request = DatabaseQueryRequest(
            question=str(scenario["question"]),
            database_source_id=str(scenario["database_source_id"]),
            table_names=[str(scenario["table_name"])],
            limit=int(scenario["limit"]),
        )
        database_before = _source_snapshot(database_path)
        files_before = _file_snapshot(root)
        registry_boundary = _source_registry_boundary()
        original_route = nl2sql_service.route_database_tables
        original_source = nl2sql_service.get_database_source
        original_vanna = nl2sql_service.build_vanna_client_from_app_config
        original_url = sql_runner.database_source_url
        original_create_engine = sql_runner.create_async_engine
        engine_proxies: list[_EngineProxy] = []

        def create_fixture_engine(_url: str, **_kwargs: Any) -> _EngineProxy:
            proxy = _EngineProxy(create_async_engine(f"sqlite+aiosqlite:///{database_path}", poolclass=NullPool))
            engine_proxies.append(proxy)
            return proxy

        nl2sql_service.route_database_tables = lambda _session, _request: _async_value(route)
        nl2sql_service.get_database_source = lambda _session, _source_id: _async_value(source)
        nl2sql_service.build_vanna_client_from_app_config = lambda: _FixedGoldenVanna()
        sql_runner.database_source_url = lambda _source: f"sqlite+aiosqlite:///{database_path}"
        sql_runner.create_async_engine = create_fixture_engine
        try:
            result = await nl2sql_service.query_database_knowledge(None, request)
        finally:
            nl2sql_service.route_database_tables = original_route
            nl2sql_service.get_database_source = original_source
            nl2sql_service.build_vanna_client_from_app_config = original_vanna
            sql_runner.database_source_url = original_url
            sql_runner.create_async_engine = original_create_engine
            for proxy in engine_proxies:
                await proxy.dispose()
        database_after = _source_snapshot(database_path)
        files_after = _file_snapshot(root)

    expected_rows = scenario["expected_answer_rows"]
    if result.sql != scenario["expected_sql"]:
        raise AssertionError(f"unexpected final SQL: {result.sql}")
    if result.execution.rows != expected_rows or not result.execution.is_complete:
        raise AssertionError("legacy NL2SQL execution did not produce expected complete rows")
    if database_before != database_after:
        raise AssertionError("read-only execution changed the analytics fixture")

    stable = _stable_result(result)
    return {
        "result": stable,
        "evidence": {
            "generated_sql": result.sql,
            "validated_table_scope": result.route.table_names,
            "provider_calls": result.generation.get("llm_call_budget"),
            "references_count": {
                key: int((value or {}).get("count") or 0)
                for key, value in result.references.items()
                if isinstance(value, dict) and "count" in value
            },
            "read_only_transaction_adapter": "test_only_sqlite_adapter_for_postgresql_session_controls",
            "production_source_registry": registry_boundary,
            "failure_checks": {
                "unregistered_table": _validation_case(
                    "SELECT * FROM other_facts", [str(scenario["table_name"])]
                ),
                "write_statement": _validation_case(
                    "DELETE FROM sales_facts", [str(scenario["table_name"])]
                ),
            },
            "implementation_dependencies": _implementation_dependencies(),
        },
        "database_side_effects": {
            "business_rows_written": 0,
            "before": database_before,
            "after": database_after,
            "unchanged": database_before == database_after,
        },
        "filesystem_side_effects": {
            "business_writes": [] if files_before == files_after else ["fixture_root_changed"],
            "fixture_root_unchanged": files_before == files_after,
            "before": files_before,
            "after": files_after,
        },
        "provider_revision": "legacy-nl2sql-readonly-v1",
        "failure_semantics": "unregistered_table->validation_rejected; write_statement->validation_rejected; valid_sql->complete_rows",
        "sanitized_fixture_manifest": {"fixtures": ["docs/knowledge-platform/golden-fixtures/database_nl2sql_and_readonly_execute.json"]},
    }


async def _async_value(value: Any) -> Any:
    return value


def observe() -> dict[str, Any]:
    return asyncio.run(_observe_async(_fixture()))


if __name__ == "__main__":
    print(json.dumps(observe(), ensure_ascii=False, indent=2, sort_keys=True, default=str))
