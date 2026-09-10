from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from knowledge_contracts import Principal
from knowledge_platform.catalog import migrate_to_latest
from knowledge_platform.local.package_import import LocalPackagePublisher
from knowledge_platform.package.builder import KnowledgePackageBuilder, export_package_zip


def _catalog(path: Path) -> None:
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as c:
        migrate_to_latest(c)
        c.exec_driver_sql("INSERT INTO knowledge_spaces(id,name,description,permissions_json,created_at,updated_at) VALUES ('s','S','','{}','now','now')")
    engine.dispose()


def _package(tmp_path: Path) -> Path:
    source = {"id": "source", "space_id": "s", "dataset_id": "legacy-dataset", "dialect": "postgresql", "ddl": [{"id": "ddl", "content": "CREATE TABLE sales(id int)"}], "documentation": [], "sql_examples": [], "entities": []}
    root = tmp_path / "root"
    KnowledgePackageBuilder().build(output_dir=root, package_id="db", version="1", spaces=[{"id": "s", "name": "S"}], collections=[{"id": "c", "space_id": "s", "name": "DB", "version": "1", "kind": "database", "asset_ids": [], "database_source_ids": ["source"], "capabilities": ["database_nl2sql"]}], assets=[], asset_files={}, capabilities=["database_nl2sql"], catalog_revision="sha256:" + "a" * 64, database_sources=[source])
    archive = tmp_path / "db.zip"; export_package_zip(root, archive); return archive


def test_database_source_publishes_and_replays_without_live_connection(tmp_path: Path) -> None:
    archive = _package(tmp_path); catalog = tmp_path / "catalog.db"; _catalog(catalog)
    digest = "sha256:" + hashlib.sha256(archive.read_bytes()).hexdigest()
    principal = Principal("admin", scopes=("knowledge.admin", "knowledge.space:s"))
    with LocalPackagePublisher(catalog, tmp_path / "state") as publisher:
        first = asyncio.run(publisher.import_package(principal, archive, digest))
        assert first["idempotent"] is False
        source = publisher.read_database_source("source", first["package_revision"])
        assert source["dataset_id"] == "legacy-dataset"
        collection = publisher.read_database_collection("s", "c", "1", first["package_revision"])
        assert collection["database_source_ids"] == ["source"]
        with sqlite3.connect(catalog) as db:
            db.execute("UPDATE knowledge_datasets SET asset_ids='[]', semantic_asset_ids='[]', capabilities='[ \"database_nl2sql\" ]' WHERE id='c'")
            db.commit()
        assert publisher.read_database_collection("s", "c", "1", first["package_revision"])["id"] == "c"
        second = asyncio.run(publisher.import_package(principal, archive, digest))
        assert second["idempotent"] is True
    with sqlite3.connect(catalog) as db:
        assert db.execute("SELECT COUNT(*) FROM knowledge_package_database_collections").fetchone()[0] == 1


def test_database_source_rollback_and_tamper_are_fail_closed(tmp_path: Path) -> None:
    archive = _package(tmp_path); catalog = tmp_path / "catalog.db"; _catalog(catalog)
    digest = "sha256:" + hashlib.sha256(archive.read_bytes()).hexdigest()
    principal = Principal("admin", scopes=("knowledge.admin", "knowledge.space:s"))
    with LocalPackagePublisher(catalog, tmp_path / "state") as publisher:
        publisher._before_commit = lambda _db: (_ for _ in ()).throw(RuntimeError("injected"))  # type: ignore[method-assign]
        with pytest.raises(RuntimeError):
            asyncio.run(publisher.import_package(principal, archive, digest))
        with sqlite3.connect(catalog) as db:
            assert db.execute("SELECT COUNT(*) FROM knowledge_package_database_sources").fetchone()[0] == 0
        publisher._before_commit = lambda _db: None  # type: ignore[method-assign]
        asyncio.run(publisher.import_package(principal, archive, digest))
        with sqlite3.connect(catalog) as db:
            db.execute("UPDATE knowledge_package_database_sources SET source_json='{}' WHERE id='source'"); db.commit()
        with pytest.raises(ValueError, match="publication is incomplete"):
            asyncio.run(publisher.import_package(principal, archive, digest))
