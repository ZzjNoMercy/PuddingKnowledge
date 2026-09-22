from __future__ import annotations

import hashlib
import json
import sqlite3
import zipfile
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from knowledge_contracts import BlobReadResult, Correlation, Principal
from knowledge_platform.local.package_export import PackageExportError, PackageExportService
from knowledge_platform.package import validate_package
from knowledge_platform.retrieval.local import LocalFilesystemBlobReader


def digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


class Repo:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.revision = snapshot.catalog_revision

    def read_package_snapshot(self):
        return self.snapshot

    @property
    def catalog_revision(self):
        return self.revision


class Reader:
    def __init__(self, values, repo=None):
        self.values, self.repo = values, repo

    async def read(self, request):
        if self.repo and request.resource_uri == "knowledge://spaces/s/assets/a":
            self.repo.revision = "sha256:" + "f" * 64
        content = self.values[request.resource_uri]
        return BlobReadResult(resource_uri=request.resource_uri, content=content, content_digest=digest(content), start=0, end=len(content), asset_digest=digest(content))


def snapshot(content: bytes = b"alpha"):
    revision = "sha256:" + "a" * 64
    return SimpleNamespace(
        catalog_revision=revision,
        spaces=[{"id": "s", "name": "Space"}],
        collections=[{"id": "c", "version": "v1", "space_id": "s", "name": "Docs", "kind": "document", "asset_ids": ["a"], "semantic_asset_ids": [], "capabilities": ["knowledge_read"]}],
        assets=[{"id": "a", "space_id": "s", "kind": "document", "title": "A", "description": "", "mime_type": "text/plain", "source_type": "local", "source_uri": "knowledge://spaces/s/assets/a", "content_digest": digest(content)}],
        semantic_assets=[], database_sources=[], provider_versions={},
    )


def principal() -> Principal:
    return Principal("admin", ("knowledge.admin", "knowledge.space:s"))


def output(tmp_path: Path, name: str) -> Path:
    path = tmp_path / f"knowledge-export-test-{name}"
    if path.exists():
        path.unlink()
    return path


@pytest.mark.asyncio
async def test_export_reads_selected_collection_and_publishes_zip(tmp_path: Path):
    snap = snapshot()
    repo = Repo(snap)
    destination = output(tmp_path, "out.zip")
    result = await PackageExportService(repo, Reader({"knowledge://spaces/s/assets/a": b"alpha"})).export(
        principal(), Correlation("trace1"), output_zip=destination, package_id="pkg", version="1", collections=[{"id": "c", "version": "v1"}]
    )
    assert result["asset_count"] == 1
    with zipfile.ZipFile(destination) as archive:
        assert archive.read("assets/originals/a") == b"alpha"
    assert result["package_revision"].startswith("sha256:")


@pytest.mark.asyncio
async def test_rejects_non_admin_or_wrong_space_without_output(tmp_path: Path):
    service = PackageExportService(Repo(snapshot()), Reader({"knowledge://spaces/s/assets/a": b"alpha"}))
    with pytest.raises(PackageExportError, match="admin"):
        await service.export(Principal("x", ("knowledge.space:s",)), Correlation("trace1"), output_zip=output(tmp_path, "x.zip"), package_id="pkg", version="1", collections=[{"id": "c", "version": "v1"}])
    with pytest.raises(PackageExportError, match="Space scope"):
        await service.export(Principal("x", ("knowledge.admin", "space:other")), Correlation("trace1"), output_zip=output(tmp_path, "x.zip"), package_id="pkg", version="1", collections=[{"id": "c", "version": "v1"}])


@pytest.mark.asyncio
async def test_digest_or_catalog_fence_failure_does_not_publish(tmp_path: Path):
    snap = snapshot()
    repo = Repo(snap)
    service = PackageExportService(repo, Reader({"knowledge://spaces/s/assets/a": b"changed"}))
    with pytest.raises(PackageExportError, match="digest"):
        await service.export(principal(), Correlation("trace1"), output_zip=output(tmp_path, "x.zip"), package_id="pkg", version="1", collections=[{"id": "c", "version": "v1"}])
    repo = Repo(snap)
    service = PackageExportService(repo, Reader({"knowledge://spaces/s/assets/a": b"alpha"}, repo))
    with pytest.raises(PackageExportError, match="Catalog changed"):
        await service.export(principal(), Correlation("trace1"), output_zip=output(tmp_path, "y.zip"), package_id="pkg", version="1", collections=[{"id": "c", "version": "v1"}])


@pytest.mark.asyncio
async def test_live_database_without_portable_evidence_is_rejected(tmp_path: Path):
    snap = snapshot()
    snap.collections[0]["kind"] = "live_database"
    with pytest.raises(PackageExportError, match="portable evidence"):
        await PackageExportService(Repo(snap), Reader({"knowledge://spaces/s/assets/a": b"alpha"})).export(principal(), Correlation("trace1"), output_zip=output(tmp_path, "db.zip"), package_id="pkg", version="1", collections=[{"id": "c", "version": "v1"}])


@pytest.mark.asyncio
async def test_sqlite_catalog_metadata_closure_exports_original_normalized_and_image(tmp_path: Path):
    original, normalized, image = b"raw", b"normalized", b"png-bytes"
    snap = snapshot(original)
    snap.collections[0]["asset_ids"] = ["orig"]
    snap.assets = [
        {"id": "orig", "space_id": "s", "kind": "wiki_page", "title": "Raw", "description": "", "mime_type": "text/markdown", "source_type": "local", "source_uri": "knowledge://spaces/s/assets/orig", "content_digest": digest(original)},
        {"id": "norm", "space_id": "s", "kind": "document", "title": "Normalized", "description": "", "mime_type": "text/markdown", "source_type": "local", "source_uri": "knowledge://spaces/s/assets/norm", "content_digest": digest(normalized)},
        {"id": "image", "space_id": "s", "kind": "image", "title": "Image", "description": "", "mime_type": "image/png", "source_type": "local", "source_uri": "knowledge://spaces/s/assets/image", "content_digest": digest(image)},
    ]
    database = tmp_path / "package-export-catalog.sqlite"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE knowledge_assets (id TEXT, space_id TEXT, metadata_json TEXT)")
    connection.executemany("INSERT INTO knowledge_assets VALUES (?, ?, ?)", [("orig", "s", json.dumps({"derivative_asset_ids": ["norm", "image"]})), ("norm", "s", "{}"), ("image", "s", "{}")])
    connection.commit(); connection.close()
    repo = Repo(snap); repo._database_path = database
    files = {}
    for asset_id, content in (("orig", original), ("norm", normalized), ("image", image)):
        path = tmp_path / f"{database.stem}-{asset_id}"
        path.write_bytes(content); files[asset_id] = path
    destination = output(tmp_path, "relations.zip")
    await PackageExportService(repo, LocalFilesystemBlobReader(files)).export(principal(), Correlation("trace1"), output_zip=destination, package_id="pkg", version="1", collections=[{"id": "c", "version": "v1"}])
    with zipfile.ZipFile(destination) as archive:
        names = set(archive.namelist())
        assert {"assets/originals/orig.md", "assets/originals/norm.md", "assets/originals/image.png"} <= names
