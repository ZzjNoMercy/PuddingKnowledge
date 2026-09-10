from __future__ import annotations

import hashlib
import json
import sqlite3
import zipfile
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from knowledge_contracts import Correlation, Principal
from knowledge_platform.catalog.sqlite_query import SqliteCatalogQueryRepository
from knowledge_platform.local.package_export import PackageExportError, PackageExportService
from knowledge_platform.local.package_import import LocalPackagePublisher
from knowledge_platform.catalog import migrate_to_latest
from knowledge_platform.package import KnowledgePackageBuilder, PackageBuildError


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _source(source_id: str, space: str, dataset: str) -> dict:
    return {"id": source_id, "space_id": space, "dataset_id": dataset, "dialect": "postgresql", "ddl": [{"id": "ddl1", "content": "CREATE TABLE sales (amount integer)"}], "documentation": [], "sql_examples": [], "entities": []}


def _db(path: Path, sources: list[tuple], *, binding_dataset: str = "dataset_sales") -> None:
    db = sqlite3.connect(path)
    db.executescript("""
      CREATE TABLE knowledge_spaces (id TEXT, name TEXT, description TEXT);
      CREATE TABLE knowledge_datasets (id TEXT, space_id TEXT, name TEXT, version TEXT, kind TEXT, capabilities TEXT, freshness TEXT, asset_ids TEXT, semantic_asset_ids TEXT);
      CREATE TABLE knowledge_assets (id TEXT, space_id TEXT, kind TEXT, title TEXT, description TEXT, mime_type TEXT, source_type TEXT, source_uri TEXT, revision TEXT, content_digest TEXT);
      CREATE TABLE knowledge_collection_bindings (space_id TEXT, collection_id TEXT, collection_version TEXT, capability TEXT, binding_json TEXT);
      CREATE TABLE knowledge_package_database_sources (id TEXT PRIMARY KEY, space_id TEXT, dataset_id TEXT, package_revision TEXT, source_json TEXT, content_digest TEXT);
      CREATE TABLE knowledge_package_imports (package_revision TEXT PRIMARY KEY, status TEXT);
    """)
    db.execute("INSERT INTO knowledge_spaces VALUES ('space_sales','Sales','')")
    db.execute("INSERT INTO knowledge_spaces VALUES ('space_other','Other','')")
    db.execute("INSERT INTO knowledge_datasets VALUES ('collection_sales','space_sales','Sales','v1','database','[\"database_nl2sql\"]','{}','[]','[]')")
    db.execute("INSERT INTO knowledge_collection_bindings VALUES ('space_sales','collection_sales','v1','database_nl2sql',?)", (json.dumps({"dataset_id": binding_dataset}),))
    for source_id, space, dataset, payload, revision in sources:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        db.execute("INSERT INTO knowledge_package_database_sources VALUES (?,?,?,?,?,?)", (source_id, space, dataset, revision, json.dumps(payload, ensure_ascii=False), _digest(encoded)))
        db.execute("INSERT INTO knowledge_package_imports VALUES (?, 'complete')", (revision,))
    db.commit(); db.close()


@pytest.mark.asyncio
async def test_database_package_exports_only_bound_selected_source(tmp_path: Path) -> None:
    sales = _source("source_sales", "space_sales", "dataset_sales")
    other = _source("source_other", "space_other", "dataset_other")
    db_path = tmp_path / "catalog.sqlite"
    _db(db_path, [("source_sales", "space_sales", "dataset_sales", sales, "sha256:" + "a" * 64), ("source_other", "space_other", "dataset_other", other, "sha256:" + "b" * 64)])
    repo = SqliteCatalogQueryRepository(db_path)
    snapshot = repo.read_package_snapshot()
    assert [item["id"] for item in snapshot.database_sources] == ["source_sales"]
    destination = Path("/private/tmp") / f"database-package-{tmp_path.name}.zip"
    if destination.exists(): destination.unlink()
    await PackageExportService(repo, _NoopReader()).export(Principal("admin", ("knowledge.admin", "knowledge.space:space_sales")), Correlation("db-export"), output_zip=destination, package_id="db", version="v1", collections=[{"id": "collection_sales", "version": "v1"}])
    with zipfile.ZipFile(destination) as archive:
        document = json.loads(archive.read("database/index.json"))
        collections = json.loads(archive.read("collections/index.json"))
    assert [item["id"] for item in document["sources"]] == ["source_sales"]
    assert collections["collections"][0]["database_source_ids"] == ["source_sales"]


@pytest.mark.asyncio
async def test_exported_database_package_imports_with_portable_collection_relation(tmp_path: Path) -> None:
    source = _source("source_sales", "space_sales", "dataset_sales")
    db_path = tmp_path / "catalog.sqlite"
    _db(db_path, [("source_sales", "space_sales", "dataset_sales", source, "sha256:" + "a" * 64)])
    destination = Path("/private/tmp") / f"database-roundtrip-{tmp_path.name}.zip"
    if destination.exists(): destination.unlink()
    await PackageExportService(SqliteCatalogQueryRepository(db_path), _NoopReader()).export(Principal("admin", ("knowledge.admin", "knowledge.space:space_sales")), Correlation("roundtrip"), output_zip=destination, package_id="db", version="v1", collections=[{"id": "collection_sales", "version": "v1"}])
    target = tmp_path / "target.sqlite"
    engine = create_engine(f"sqlite:///{target}")
    with engine.begin() as connection:
        migrate_to_latest(connection)
        connection.exec_driver_sql("INSERT INTO knowledge_spaces(id,name,description,permissions_json,created_at,updated_at) VALUES ('space_sales','Sales','','{}','now','now')")
    engine.dispose()
    digest = _digest(destination.read_bytes())
    with LocalPackagePublisher(target, tmp_path / "state") as publisher:
        result = await publisher.import_package(Principal("admin", ("knowledge.admin", "knowledge.space:space_sales")), destination, digest)
        relation = publisher.read_database_collection("space_sales", "collection_sales", "v1", result["package_revision"])
        assert relation["database_source_ids"] == ["source_sales"]


def test_collection_database_source_ids_are_explicit_and_builder_rejects_dangling_or_cross_space(tmp_path: Path) -> None:
    source = _source("source_sales", "space_sales", "dataset_sales")
    db_path = tmp_path / "catalog.sqlite"
    _db(db_path, [("source_sales", "space_sales", "dataset_sales", source, "sha256:" + "a" * 64)])
    db = sqlite3.connect(db_path)
    db.execute("CREATE TABLE knowledge_package_database_collections (space_id TEXT, collection_id TEXT, collection_version TEXT, source_id TEXT)")
    db.execute("INSERT INTO knowledge_package_database_collections VALUES ('space_sales','collection_sales','v1','source_sales')")
    db.commit(); db.close()
    snapshot = SqliteCatalogQueryRepository(db_path).read_package_snapshot()
    assert snapshot.collections[0]["database_source_ids"] == ["source_sales"]
    common = {"id": "collection_sales", "space_id": "space_sales", "name": "Sales", "version": "v1", "kind": "database", "asset_ids": [], "capabilities": ["database_nl2sql"]}
    with pytest.raises(PackageBuildError, match="unknown database source"):
        KnowledgePackageBuilder().build(output_dir=tmp_path / "dangling", package_id="p", version="1", spaces=[{"id": "space_sales", "name": "Sales"}], collections=[{**common, "database_source_ids": ["missing"]}], assets=[], asset_files={}, capabilities=["database_nl2sql"], catalog_revision="sha256:" + "a" * 64, database_sources=[])
    with pytest.raises(PackageBuildError, match="another Space"):
        KnowledgePackageBuilder().build(output_dir=tmp_path / "cross", package_id="p", version="1", spaces=[{"id": "space_sales", "name": "Sales"}, {"id": "space_other", "name": "Other"}], collections=[{**common, "database_source_ids": ["source_other"]}], assets=[], asset_files={}, capabilities=["database_nl2sql"], catalog_revision="sha256:" + "a" * 64, database_sources=[{**source, "id": "source_other", "space_id": "space_other", "dataset_id": "dataset_other"}])


def test_database_source_digest_tamper_is_rejected(tmp_path: Path) -> None:
    source = _source("source_sales", "space_sales", "dataset_sales")
    db_path = tmp_path / "catalog.sqlite"
    _db(db_path, [("source_sales", "space_sales", "dataset_sales", source, "sha256:" + "a" * 64)])
    db = sqlite3.connect(db_path); db.execute("UPDATE knowledge_package_database_sources SET source_json=?", (json.dumps({**source, "ddl": []}),)); db.commit(); db.close()
    with pytest.raises(ValueError, match="digest"):
        SqliteCatalogQueryRepository(db_path).read_package_snapshot()


def test_database_source_requires_complete_import_and_full_revision_digest(tmp_path: Path) -> None:
    source = _source("source_sales", "space_sales", "dataset_sales")
    db_path = tmp_path / "catalog.sqlite"
    _db(db_path, [("source_sales", "space_sales", "dataset_sales", source, "sha256:" + "a" * 63 + "g")])
    with pytest.raises(ValueError, match="revision"):
        SqliteCatalogQueryRepository(db_path).read_package_snapshot()
    db = sqlite3.connect(db_path); db.execute("UPDATE knowledge_package_imports SET status='staged'"); db.execute("UPDATE knowledge_package_database_sources SET package_revision=?", ("sha256:" + "a" * 64,)); db.commit(); db.close()
    with pytest.raises(ValueError):
        SqliteCatalogQueryRepository(db_path).read_package_snapshot()


class _NoopReader:
    async def read(self, request):
        raise AssertionError("database-only package should not read blob assets")


@pytest.mark.asyncio
async def test_export_refuses_partially_missing_explicit_sources(tmp_path):
    source = _source('source_sales', 'space_sales', 'dataset_sales')
    path = tmp_path / 'catalog.db'
    _db(path, [('source_sales', 'space_sales', 'dataset_sales', source, 'sha256:' + 'a' * 64)])
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE knowledge_package_database_collections(space_id TEXT, collection_id TEXT, collection_version TEXT, source_id TEXT)')
        db.executemany('INSERT INTO knowledge_package_database_collections VALUES(?,?,?,?)',
            [('space_sales','collection_sales','v1',value) for value in ['source_sales','missing']])
    exporter = PackageExportService(SqliteCatalogQueryRepository(path), None)
    with pytest.raises(PackageExportError, match='missing portable evidence'):
        await exporter.export(principal=Principal('admin',scopes=('knowledge.admin','knowledge.space:space_sales')),
            correlation=Correlation('missing-source'),output_zip=tmp_path/'output.zip',package_id='x',version='1',
            collections=[{'id':'collection_sales','version':'v1'}])
    assert not (tmp_path/'output.zip').exists()
