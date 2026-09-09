from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, select


def _catalog(tmp_path):
    from knowledge_platform.catalog import (
        KnowledgeQueryResult,
        KnowledgeSpace,
        migrate_to_latest,
    )

    database = tmp_path / "platform.sqlite3"
    engine = create_engine(f"sqlite:///{database}")
    with engine.begin() as connection:
        migrate_to_latest(connection)
        connection.execute(
            KnowledgeSpace.__table__.insert().values(
                id="space_1",
                name="Local",
                description="",
                permissions_json={},
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
        )
        connection.execute(
            KnowledgeSpace.__table__.insert().values(
                id="space_2",
                name="Second",
                description="",
                permissions_json={},
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
        )
        connection.execute(
            KnowledgeQueryResult.__table__.insert().values(
                id="query_result_1",
                status="ready",
                question="local question",
                sql_digest="sha256:" + "1" * 64,
                columns_json=["name"],
                row_count=1,
                profile_json={},
                artifact_uri="knowledge://query-results/query_result_1/artifact",
                artifact_reference_digest="sha256:" + "2" * 64,
                artifact_format="jsonl",
                correlation_json={},
                created_at=datetime.now(timezone.utc),
                expires_at=datetime(2026, 9, 6, tzinfo=timezone.utc),
            )
        )
    return database, engine


def test_query_result_scope_store_binds_once_and_reads_explicit_space(tmp_path) -> None:
    from knowledge_platform.catalog import KnowledgeQueryResultScope
    from knowledge_platform.catalog.query_result_scope import SqliteQueryResultScopeStore

    database, engine = _catalog(tmp_path)
    store = SqliteQueryResultScopeStore(database)

    bound = store.bind_query_result(
        query_result_id="query_result_1",
        space_id="space_1",
        bound_at="2026-09-05T00:00:00+00:00",
    )
    assert bound == {
        "query_result_id": "query_result_1",
        "space_id": "space_1",
        "bound_at": "2026-09-05T00:00:00+00:00",
    }
    assert store.get_space_id(query_result_id="query_result_1") == "space_1"
    assert store.bind_query_result(query_result_id="query_result_1", space_id="space_1") == bound

    with engine.connect() as connection:
        rows = connection.execute(select(KnowledgeQueryResultScope.__table__)).all()
    assert len(rows) == 1
    engine.dispose()


def test_query_result_scope_store_rejects_missing_objects_and_rebinding(tmp_path) -> None:
    from knowledge_platform.catalog.query_result_scope import SqliteQueryResultScopeStore

    database, engine = _catalog(tmp_path)
    store = SqliteQueryResultScopeStore(database)

    with pytest.raises(LookupError, match="Space does not exist"):
        store.bind_query_result(query_result_id="query_result_1", space_id="space_missing")
    with pytest.raises(LookupError, match="QueryResult does not exist"):
        store.bind_query_result(query_result_id="query_result_missing", space_id="space_1")

    store.bind_query_result(query_result_id="query_result_1", space_id="space_1")
    with pytest.raises(ValueError, match="already bound"):
        store.bind_query_result(query_result_id="query_result_1", space_id="space_2")
    with pytest.raises(ValueError, match="timestamp"):
        store.bind_query_result(query_result_id="query_result_1", space_id="space_1", bound_at="not-a-time")
    assert store.get_space_id(query_result_id="query_result_missing") is None
    engine.dispose()


def test_query_result_scope_store_rejects_symlink_database(tmp_path) -> None:
    from knowledge_platform.catalog.query_result_scope import SqliteQueryResultScopeStore

    database, engine = _catalog(tmp_path)
    engine.dispose()
    alias = tmp_path / "alias.sqlite3"
    alias.symlink_to(database)
    with pytest.raises(FileNotFoundError):
        SqliteQueryResultScopeStore(alias)
