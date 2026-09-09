from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from knowledge_platform.catalog import migrate_to_latest
from knowledge_platform.wiki import SqliteWikiCompilationJobStore


def _catalog(path: Path) -> None:
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as connection:
        migrate_to_latest(connection)
    engine.dispose()


def test_sqlite_wiki_job_store_survives_new_store_and_keeps_key_secret_out(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite3"
    _catalog(path)
    key = "compile-" + "secret-looking-value"
    first_store = SqliteWikiCompilationJobStore(database_path=path, space_id="space_local")
    second_store = SqliteWikiCompilationJobStore(database_path=path, space_id="space_local")

    assert asyncio.run(first_store.claim(idempotency_key=key)).acquired is True
    assert asyncio.run(second_store.claim(idempotency_key=key)).acquired is False
    asyncio.run(first_store.complete(
        idempotency_key=key,
        resource_uri="knowledge://spaces/space_local/wiki/asset_source",
    ))
    completed = asyncio.run(second_store.claim(idempotency_key=key))

    assert completed.existing_resource_uri == "knowledge://spaces/space_local/wiki/asset_source"
    raw = path.read_bytes()
    assert key.encode() not in raw
    assert b"wiki_compile" in raw


def test_sqlite_wiki_job_store_does_not_cross_space_reuse_an_idempotency_key(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite3"
    _catalog(path)
    key = "same-key-in-different-space"
    first = SqliteWikiCompilationJobStore(database_path=path, space_id="space_a")
    other = SqliteWikiCompilationJobStore(database_path=path, space_id="space_b")
    assert asyncio.run(first.claim(idempotency_key=key)).acquired is True
    asyncio.run(first.complete(idempotency_key=key, resource_uri="knowledge://spaces/space_a/wiki/asset_a"))
    with pytest.raises(ValueError, match="another Space"):
        asyncio.run(other.claim(idempotency_key=key))


def test_sqlite_wiki_job_store_releases_failed_claim_and_rejects_bad_terminal_uri(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite3"
    _catalog(path)
    store = SqliteWikiCompilationJobStore(database_path=path, space_id="space_local")
    key = "compile-retry"
    assert asyncio.run(store.claim(idempotency_key=key)).acquired is True
    asyncio.run(store.release(idempotency_key=key))
    assert asyncio.run(store.claim(idempotency_key=key)).acquired is True
    with pytest.raises(ValueError, match="resource URI"):
        asyncio.run(store.complete(idempotency_key=key, resource_uri="file:///tmp/wiki.md"))
    asyncio.run(store.release(idempotency_key=key))


def test_sqlite_wiki_job_store_requires_platform_schema_and_space(tmp_path: Path) -> None:
    path = tmp_path / "empty.sqlite3"
    path.touch()
    store = SqliteWikiCompilationJobStore(database_path=path, space_id="space_local")
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        asyncio.run(store.claim(idempotency_key="compile-missing-schema"))
    with pytest.raises(ValueError):
        SqliteWikiCompilationJobStore(database_path=path, space_id="space/unsafe")
