from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest
import sqlite3
from sqlalchemy import create_engine

from knowledge_contracts import BlobReadRequest, Correlation, Principal
from knowledge_platform.local.package_import import LocalPackagePublisher, PackageBlobReader, PackageRetrievalProvider
from knowledge_platform.retrieval.ports import RetrievalProviderError
from knowledge_platform.catalog import migrate_to_latest
from knowledge_platform.package.builder import KnowledgePackageBuilder, export_package_zip


def _package(tmp_path: Path, *, space: str = "space_1", asset_id: str = "asset_1", body: bytes = b"# Package\nhello", semantic: bool = False) -> Path:
    source = tmp_path / f"{asset_id}.md"
    source.write_bytes(body)
    digest = "sha256:" + hashlib.sha256(body).hexdigest()
    root = tmp_path / f"pkg-{asset_id}"
    KnowledgePackageBuilder().build(
        output_dir=root,
        package_id="fixture",
        version="1",
        spaces=[{"id": space, "name": "Space", "description": ""}],
        collections=[{"id": "collection_1", "space_id": space, "name": "Docs", "version": "1", "kind": "wiki", "asset_ids": [asset_id], "semantic_asset_ids": ["measure:sales"] if semantic else [], "capabilities": ["knowledge_read", "knowledge_search", "wiki_query"]}],
        assets=[{"id": asset_id, "space_id": space, "kind": "wiki_page", "title": "Page", "description": "", "mime_type": "text/markdown", "source_type": "wiki", "source_uri": f"knowledge://spaces/{space}/assets/{asset_id}", "revision": digest, "content_digest": digest}],
        asset_files={asset_id: source},
        capabilities=["knowledge_read", "knowledge_search", "wiki_query"],
        catalog_revision="sha256:" + "0" * 64,
        semantic_assets=[{"id": "measure:sales", "type": "measure", "name": "Sales", "body": "# Sales\nRevenue measure."}] if semantic else [],
    )
    archive = tmp_path / f"{asset_id}.zip"
    export_package_zip(root, archive)
    return archive


def _principal(*spaces: str, tenant: str | None = None) -> Principal:
    return Principal("admin", tenant_id=tenant, scopes=("knowledge.admin", *(f"knowledge.space:{s}" for s in spaces)))


def _catalog(path: Path) -> None:
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as connection:
        migrate_to_latest(connection)
        connection.exec_driver_sql("INSERT INTO knowledge_spaces(id,name,description,permissions_json,created_at,updated_at) VALUES ('space_1','Space','','{}','now','now')")
    engine.dispose()


def _digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def test_import_survives_restart_and_same_revision_is_idempotent(tmp_path: Path) -> None:
    archive = _package(tmp_path)
    _catalog(tmp_path / "catalog.sqlite3")
    publisher = LocalPackagePublisher(tmp_path / "catalog.sqlite3", tmp_path / "state")
    first = asyncio.run(publisher.import_package(_principal("space_1"), archive, _digest(archive)))
    assert first["idempotent"] is False
    assert publisher.read_published("asset_1") == b"# Package\nhello"
    publisher.close()
    restarted = LocalPackagePublisher(tmp_path / "catalog.sqlite3", tmp_path / "state")
    second = asyncio.run(restarted.import_package(_principal("space_1"), archive, _digest(archive)))
    assert second["idempotent"] is True
    assert restarted.read_published("asset_1") == b"# Package\nhello"
    restarted.close()


def test_import_requires_all_space_scopes_and_rejects_tenant(tmp_path: Path) -> None:
    archive = _package(tmp_path)
    _catalog(tmp_path / "catalog.sqlite3")
    publisher = LocalPackagePublisher(tmp_path / "catalog.sqlite3", tmp_path / "state")
    with pytest.raises(PermissionError):
        asyncio.run(publisher.import_package(_principal(), archive, _digest(archive)))
    with pytest.raises(PermissionError):
        asyncio.run(publisher.import_package(_principal("space_1", tenant="tenant"), archive, _digest(archive)))
    publisher.close()


def test_corrupt_zip_and_object_are_refused(tmp_path: Path) -> None:
    archive = _package(tmp_path)
    _catalog(tmp_path / "catalog.sqlite3")
    publisher = LocalPackagePublisher(tmp_path / "catalog.sqlite3", tmp_path / "state")
    broken = tmp_path / "broken.zip"
    broken.write_bytes(archive.read_bytes()[:-8])
    with pytest.raises(Exception):
        asyncio.run(publisher.import_package(_principal("space_1"), broken, _digest(broken)))
    asyncio.run(publisher.import_package(_principal("space_1"), archive, _digest(archive)))
    object_path = next(p for p in (tmp_path / "state" / "objects").iterdir() if len(p.name) == 64)
    object_path.write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="integrity"):
        publisher.read_published("asset_1")
    publisher.close()


def test_conflict_and_transaction_failure_leave_no_partial_rows(tmp_path: Path) -> None:
    archive = _package(tmp_path)
    _catalog(tmp_path / "catalog.sqlite3")
    publisher = LocalPackagePublisher(tmp_path / "catalog.sqlite3", tmp_path / "state")

    def fail(_connection):
        raise RuntimeError("injected failure")

    publisher._before_commit = fail  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        asyncio.run(publisher.import_package(_principal("space_1"), archive, _digest(archive)))
    with sqlite3.connect(tmp_path / "catalog.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM knowledge_assets").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM knowledge_package_imports").fetchone()[0] == 0
    publisher._before_commit = lambda _connection: None  # type: ignore[method-assign]
    asyncio.run(publisher.import_package(_principal("space_1"), archive, _digest(archive)))
    other = tmp_path / "other"
    other.mkdir()
    conflict = _package(other, body=b"different")
    # A pre-existing non-package owner of the stable Asset ID is a hard conflict.
    with sqlite3.connect(tmp_path / "catalog.sqlite3") as db:
        db.execute("UPDATE knowledge_assets SET source_type='upload' WHERE id='asset_1'")
        db.commit()
    with pytest.raises(ValueError, match="Asset identity conflict"):
        asyncio.run(publisher.import_package(_principal("space_1"), conflict, _digest(conflict)))
    publisher.close()


def test_blob_reader_rejects_wrong_uri_space_and_provider_rejects_tampered_chunk(tmp_path: Path) -> None:
    archive = _package(tmp_path)
    catalog = tmp_path / "catalog.sqlite3"
    _catalog(catalog)
    publisher = LocalPackagePublisher(catalog, tmp_path / "state")
    asyncio.run(publisher.import_package(_principal("space_1"), archive, _digest(archive)))
    reader = PackageBlobReader(None, publisher)
    principal = _principal("space_1")
    request = BlobReadRequest("knowledge://spaces/other/assets/asset_1", principal, Correlation("bad-uri"), 0, 5)
    with pytest.raises(RetrievalProviderError):
        asyncio.run(reader.read(request))
    with sqlite3.connect(catalog) as db:
        db.execute("UPDATE knowledge_package_chunks SET text='forged' WHERE asset_id='asset_1'")
        db.commit()
    with pytest.raises(RetrievalProviderError):
        asyncio.run(PackageRetrievalProvider(publisher).search(query="forged", space_id="space_1", limit=10))
    publisher.close()


def test_semantic_package_is_portable_canonical_and_replay_checks_tampering(tmp_path):
    from knowledge_platform.catalog import SqliteCatalogQueryRepository
    archive = _package(tmp_path, semantic=True, body=b"MULTICHUNK " * 400)
    catalog = tmp_path / 'catalog.db'; _catalog(catalog)
    with LocalPackagePublisher(catalog, tmp_path / 'state') as publisher:
        result = asyncio.run(publisher.import_package(_principal('space_1'), archive, _digest(archive)))
        assert result['idempotent'] is False
        snapshot = SqliteCatalogQueryRepository(catalog).read_package_snapshot()
        assert len(snapshot.semantic_assets) == 1
        assert snapshot.semantic_assets[0]['id'] == 'measure:sales'
        assert 'Revenue measure.' in snapshot.semantic_assets[0]['body']
        found = asyncio.run(PackageRetrievalProvider(publisher).search(query='MULTICHUNK', space_id='space_1', limit=10))
        assert len(found) > 1
        result = asyncio.run(publisher.import_package(_principal('space_1'), archive, _digest(archive)))
        assert result['idempotent'] is True
        with sqlite3.connect(catalog) as db:
            db.execute("UPDATE knowledge_semantic_assets SET body='changed' WHERE id='measure:sales'")
        with pytest.raises(ValueError, match='semantic'):
            asyncio.run(publisher.import_package(_principal('space_1'), archive, _digest(archive)))


def test_deleted_package_chunks_are_not_a_successful_empty_query(tmp_path):
    from knowledge_platform.retrieval.ports import RetrievalIndexNotReady
    archive = _package(tmp_path); catalog = tmp_path / 'catalog.db'; _catalog(catalog)
    with LocalPackagePublisher(catalog, tmp_path / 'state') as publisher:
        asyncio.run(publisher.import_package(_principal('space_1'), archive, _digest(archive)))
        with sqlite3.connect(catalog) as db: db.execute('DELETE FROM knowledge_package_chunks')
        with pytest.raises(RetrievalIndexNotReady):
            asyncio.run(PackageRetrievalProvider(publisher).search(query='hello', space_id='space_1', limit=5))
