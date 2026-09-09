"""Replay the legacy local table query path against a fixed CSV fixture."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from knowledge_platform.baseline import normalized_digest
from schema_migrations import migrate_to_latest
from tools import pandas_knowledge_tool as pandas_tool_module

FIXTURE_PATH = Path(__file__).resolve().parents[2] / "docs/knowledge-platform/golden-fixtures/table_query.json"


def _fixture() -> dict[str, Any]:
    document = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    if document.get("format") != "agent-knowledge-platform-golden-fixture/table-query/v1":
        raise ValueError("table query fixture format is invalid")
    if document.get("sanitized") is not True or not isinstance(document.get("scenario"), dict):
        raise ValueError("table query fixture must be explicitly sanitized")
    scenario = document["scenario"]
    file_name = scenario.get("file_name")
    if (
        not isinstance(file_name, str)
        or not file_name.strip()
        or Path(file_name).is_absolute()
        or ".." in Path(file_name).parts
        or Path(file_name).name != file_name
        or Path(file_name).suffix.lower() not in {".csv", ".tsv"}
    ):
        raise ValueError("table query fixture file_name must be a simple CSV/TSV filename")
    if scenario.get("virtual_path") != f"/knowledge/imported/{file_name}":
        raise ValueError("table query fixture virtual_path must bind to the imported filename")
    return document


def _digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _file_snapshot(root: Path) -> list[dict[str, object]]:
    if not root.exists():
        return []
    return [
        {
            "path": path.relative_to(root).as_posix(),
            "bytes": path.stat().st_size,
            "content_digest": _digest(path.read_bytes()),
        }
        for path in sorted(root.rglob("*"))
        if path.is_file()
    ]


def _stable_query_result(result: dict[str, Any]) -> dict[str, object]:
    asset = result.get("asset") or {}
    return {
        "query": result.get("query"),
        "answer": result.get("answer"),
        "asset": {
            "asset_id": asset.get("asset_id") or None,
            "virtual_path": asset.get("virtual_path"),
            "file_name": asset.get("file_name"),
            "sheet_name": asset.get("sheet_name"),
            "matched_score": asset.get("matched_score"),
        }
        if asset
        else None,
        "profile": result.get("profile"),
        "engine": result.get("engine"),
    }


def _row_deltas(before: list[dict[str, object]], after: list[dict[str, object]]) -> list[dict[str, object]]:
    before_by_table = {str(item["table"]): item for item in before}
    after_by_table = {str(item["table"]): item for item in after}
    return [
        {
            "table": table,
            "before_rows": int(before_by_table.get(table, {}).get("rows", 0)),
            "after_rows": int(after_by_table.get(table, {}).get("rows", 0)),
            "delta_rows": int(after_by_table.get(table, {}).get("rows", 0))
            - int(before_by_table.get(table, {}).get("rows", 0)),
        }
        for table in sorted(set(before_by_table) | set(after_by_table))
        if int(after_by_table.get(table, {}).get("rows", 0)) != int(before_by_table.get(table, {}).get("rows", 0))
    ]


async def _database_snapshot(sessionmaker: async_sessionmaker) -> list[dict[str, object]]:
    async with sessionmaker() as session:
        table_names = sorted(
            str(row[0])
            for row in (
                await session.execute(
                    text(
                        "SELECT name FROM sqlite_master WHERE type = 'table' "
                        "AND name NOT LIKE 'sqlite_%' AND name != 'core_schema_migrations'"
                    )
                )
            ).all()
        )
        snapshot: list[dict[str, object]] = []
        for table_name in table_names:
            quoted_name = '"' + table_name.replace('"', '""') + '"'
            rows = [dict(row) for row in (await session.execute(text(f"SELECT * FROM {quoted_name}"))).mappings()]
            stable_rows = [
                {key: value for key, value in row.items() if not str(key).endswith("_at")}
                for row in rows
            ]
            snapshot.append(
                {
                    "table": table_name,
                    "rows": len(rows),
                    "content_digest": normalized_digest(stable_rows),
                }
            )
        return snapshot


class _FixedGoldenEngine(pandas_tool_module.PuddingClawPandasQueryEngine):
    """Keep the real engine/profile/runner path while fixing the model boundary."""

    def _generate_code(
        self,
        query: str,
        *,
        previous_error: str | None = None,
        previous_code: str | None = None,
    ) -> dict[str, Any]:
        return {
            "code": "result = df.groupby('品牌')['销量'].sum().sort_values(ascending=False)",
            "explanation": "按品牌分组求销量总和并降序排序",
        }

    def _synthesize_answer(self, query: str, *, code: str, rendered_result: str) -> str:
        if rendered_result.strip() != "品牌\n比亚迪    30\n蔚来      5":
            raise AssertionError("fixed table-query answer received an unexpected runner result")
        return "按品牌汇总销量：比亚迪 30，蔚来 5。"


def observe() -> dict[str, object]:
    fixture = _fixture()
    scenario = fixture["scenario"]
    with tempfile.TemporaryDirectory(prefix="puddingclaw-golden-table-") as directory:
        root = Path(directory)
        knowledge_root = root / "knowledge"
        table_path = knowledge_root / "imported" / scenario["file_name"]
        table_path.parent.mkdir(parents=True)
        table_bytes = str(scenario["content"]).encode("utf-8")
        table_path.write_bytes(table_bytes)

        database_path = root / "catalog.sqlite3"
        engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}", poolclass=NullPool)
        sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
        original_sessionmaker = pandas_tool_module.get_sessionmaker
        original_engine = pandas_tool_module.PuddingClawPandasQueryEngine
        previous_knowledge_dir = os.environ.get("PUDDINGCLAW_KNOWLEDGE_DIR")
        pandas_tool_module.get_sessionmaker = lambda: sessionmaker
        pandas_tool_module.PuddingClawPandasQueryEngine = _FixedGoldenEngine
        os.environ["PUDDINGCLAW_KNOWLEDGE_DIR"] = str(knowledge_root)
        try:
            asyncio.run(_migrate(engine))
            database_before = asyncio.run(_database_snapshot(sessionmaker))
            files_before = _file_snapshot(knowledge_root)
            tool = pandas_tool_module.PandasKnowledgeQueryTool(base_dir=str(root / "backend"))
            sync_result = tool.query_structured(
                query=str(scenario["query"]),
                file_hint=str(scenario["file_hint"]),
                preview_rows=int(scenario["preview_rows"]),
            )
            database_after_sync = asyncio.run(_database_snapshot(sessionmaker))
            async_result = asyncio.run(_query_inside_running_loop(tool, scenario))
            database_after = asyncio.run(_database_snapshot(sessionmaker))
            files_after = _file_snapshot(knowledge_root)
        finally:
            pandas_tool_module.get_sessionmaker = original_sessionmaker
            pandas_tool_module.PuddingClawPandasQueryEngine = original_engine
            if previous_knowledge_dir is None:
                os.environ.pop("PUDDINGCLAW_KNOWLEDGE_DIR", None)
            else:
                os.environ["PUDDINGCLAW_KNOWLEDGE_DIR"] = previous_knowledge_dir
            asyncio.run(engine.dispose())

    sync_asset = sync_result.get("asset") or {}
    async_asset = async_result.get("asset") or {}
    if not sync_asset or sync_result.get("answer") != "按品牌汇总销量：比亚迪 30，蔚来 5。":
        raise AssertionError("legacy table query did not produce the expected answer")
    if database_after == database_before:
        raise AssertionError("legacy table query did not populate the table catalog")
    return {
        "result": {
            "sync": _stable_query_result(sync_result),
            "async": _stable_query_result(async_result),
            "same_answer": sync_result.get("answer") == async_result.get("answer"),
        },
        "evidence": {
            "sync_asset_id": sync_asset.get("asset_id"),
            "sync_virtual_path": sync_asset.get("virtual_path"),
            "async_asset_id": async_asset.get("asset_id") or None,
            "async_virtual_path": async_asset.get("virtual_path"),
            "content_digest": _digest(table_bytes),
            "sync_candidate_count": len(sync_result.get("candidates") or []),
            "async_candidate_count": len(async_result.get("candidates") or []),
            "sync_generated_code": (sync_result.get("engine_metadata") or {}).get("generated_code"),
            "async_generated_code": (async_result.get("engine_metadata") or {}).get("generated_code"),
            "sync_result_preview": (sync_result.get("engine_metadata") or {}).get("result_preview"),
            "async_result_preview": (async_result.get("engine_metadata") or {}).get("result_preview"),
            "sync_semantic_resolution_mode": (sync_result.get("semantic_assets") or {}).get("resolution_mode"),
            "async_semantic_resolution_mode": (async_result.get("semantic_assets") or {}).get("resolution_mode"),
            "implementation_dependencies": _implementation_dependencies(),
        },
        "database_side_effects": {
            "business_rows_written": 0,
            "catalog_rows_added": sum(item["delta_rows"] for item in _row_deltas(database_before, database_after_sync)),
            "sync_catalog_row_deltas": _row_deltas(database_before, database_after_sync),
            "async_catalog_row_deltas": _row_deltas(database_after_sync, database_after),
            "before": database_before,
            "after_sync": database_after_sync,
            "after_async": database_after,
        },
        "filesystem_side_effects": {
            "business_writes": [] if files_before == files_after else ["knowledge_root_changed"],
            "knowledge_root_unchanged": files_before == files_after,
            "before": files_before,
            "after": files_after,
        },
        "provider_revision": "legacy-pandas-table-query-v1",
        "failure_semantics": "no_asset->stable_no_match; unsafe_code->execution_rejected; valid_code->answer_returned",
        "sanitized_fixture_manifest": {"fixtures": ["docs/knowledge-platform/golden-fixtures/table_query.json"]},
    }


async def _migrate(engine) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(migrate_to_latest)


async def _query_inside_running_loop(tool: pandas_tool_module.PandasKnowledgeQueryTool, scenario: dict[str, Any]) -> dict[str, Any]:
    """Mirror the async API handler, which calls the sync tool in a loop."""

    return tool.query_structured(
        query=str(scenario["query"]),
        file_hint=str(scenario["file_hint"]),
        preview_rows=int(scenario["preview_rows"]),
    )


def _implementation_dependencies() -> list[dict[str, object]]:
    repo_root = Path(__file__).resolve().parents[2]
    relative_paths = (
        "backend/scripts/phase0a_table_query_observer.py",
        "backend/api/knowledge.py",
        "backend/tools/pandas_knowledge_tool.py",
        "backend/utils/table_engine/pandas_query_engine.py",
        "backend/utils/table_engine/executor.py",
        "backend/utils/table_engine/runner.py",
        "backend/utils/table_engine/profiler.py",
    )
    return [
        {
            "path": relative,
            "content_digest": _digest((repo_root / relative).read_bytes()),
        }
        for relative in relative_paths
    ]


if __name__ == "__main__":
    print(json.dumps(observe(), ensure_ascii=False, indent=2, sort_keys=True))
