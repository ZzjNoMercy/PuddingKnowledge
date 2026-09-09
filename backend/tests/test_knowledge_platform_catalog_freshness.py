from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from knowledge_contracts import Principal
from knowledge_platform.catalog import (
    CollectionFreshnessObservation,
    SqliteCatalogQueryRepository,
    SqliteCollectionFreshnessWriter,
)


def _database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE knowledge_datasets (
                id TEXT NOT NULL, space_id TEXT NOT NULL, name TEXT NOT NULL,
                version TEXT NOT NULL, kind TEXT NOT NULL, capabilities TEXT NOT NULL,
                freshness TEXT NOT NULL, asset_ids TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY (space_id, id, version)
            );
            INSERT INTO knowledge_datasets VALUES
                ('dataset_kb_default', 'space_kb_default', 'Local Wiki', 'v1', 'wiki',
                 '["wiki_query"]', '{"mode":"local_published_wiki","keep":"yes"}', '[]', 'before');
            """
        )


def _observation(**overrides: object) -> CollectionFreshnessObservation:
    values = {
        "collection_id": "dataset_kb_default",
        "collection_version": "v1",
        "space_id": "space_kb_default",
        "capability": "wiki_query",
        "state": "ready",
        "observed_at": datetime.now(UTC).isoformat(),
        "mode": "local_published_wiki",
        "source_revision": "sha256:" + "a" * 64,
        "provider_revision": "local-wiki-1",
    }
    values.update(overrides)
    return CollectionFreshnessObservation(**values)


def _principal(*scopes: str, tenant_id: str | None = None) -> Principal:
    return Principal("freshness-test", tenant_id=tenant_id, scopes=scopes)


def test_writer_persists_observation_and_preserves_existing_metadata(tmp_path: Path) -> None:
    database = tmp_path / "catalog.sqlite3"
    _database(database)

    result = SqliteCollectionFreshnessWriter(database).observe(
        principal=_principal("knowledge.processing", "knowledge.space:space_kb_default"),
        observation=_observation(),
    )

    freshness = result["freshness"]
    assert freshness["state"] == "ready"
    assert freshness["capability"] == "wiki_query"
    assert freshness["keep"] == "yes"
    assert freshness["source_revision"].startswith("sha256:")
    assert SqliteCatalogQueryRepository(database).list_collections()[0]["freshness"] == freshness


def test_observation_rejects_bad_values_at_construction() -> None:
    with pytest.raises(ValueError, match="timezone"):
        _observation(observed_at="2026-09-04T00:00:00")
    with pytest.raises(ValueError, match="future"):
        _observation(observed_at=(datetime.now(UTC) + timedelta(minutes=6)).isoformat())
    with pytest.raises(ValueError, match="mode"):
        _observation(mode="/tmp/local")
    with pytest.raises(ValueError, match="provider_revision"):
        _observation(provider_revision="/Users/pet/private/provider")


def test_writer_rejects_scope_tenant_and_undeclared_capability(tmp_path: Path) -> None:
    database = tmp_path / "catalog.sqlite3"
    _database(database)
    writer = SqliteCollectionFreshnessWriter(database)

    with pytest.raises(PermissionError):
        writer.observe(principal=_principal("knowledge.space:space_kb_default"), observation=_observation())
    with pytest.raises(PermissionError):
        writer.observe(
            principal=_principal("knowledge.processing", "knowledge.space:space_kb_default", tenant_id="tenant-a"),
            observation=_observation(),
        )
    with pytest.raises(ValueError, match="not declared"):
        writer.observe(
            principal=_principal("knowledge.processing", "knowledge.space:space_kb_default"),
            observation=_observation(capability="table_query"),
        )

    with sqlite3.connect(database) as connection:
        stored = json.loads(connection.execute("SELECT freshness FROM knowledge_datasets").fetchone()[0])
    assert stored == {"mode": "local_published_wiki", "keep": "yes"}


def test_writer_rejects_out_of_order_observation(tmp_path: Path) -> None:
    database = tmp_path / "catalog.sqlite3"
    _database(database)
    writer = SqliteCollectionFreshnessWriter(database)
    principal = _principal("knowledge.processing", "knowledge.space:space_kb_default")
    current = datetime.now(UTC)

    writer.observe(principal=principal, observation=_observation(observed_at=current.isoformat()))
    with pytest.raises(ValueError, match="older"):
        writer.observe(
            principal=principal,
            observation=_observation(observed_at=(current - timedelta(seconds=1)).isoformat()),
        )
    stored = SqliteCatalogQueryRepository(database).list_collections()[0]["freshness"]
    assert stored["observed_at"] == current.isoformat()
