from __future__ import annotations

import hashlib
import json
import sqlite3
import zipfile
from pathlib import Path

import pytest

from knowledge_platform.catalog.sqlite_query import SqliteCatalogQueryRepository
from knowledge_contracts import Correlation, Principal
from knowledge_platform.local.package_export import PackageExportService
from knowledge_platform.retrieval.local import LocalFilesystemBlobReader


def _digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _database(path: Path, *, structured: list[tuple], catalog_asset: tuple | None = None) -> None:
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE knowledge_spaces (id TEXT PRIMARY KEY, name TEXT, description TEXT);
        CREATE TABLE knowledge_datasets (id TEXT, space_id TEXT, name TEXT, version TEXT, kind TEXT,
            capabilities TEXT, freshness TEXT, asset_ids TEXT, semantic_asset_ids TEXT);
        CREATE TABLE knowledge_assets (id TEXT PRIMARY KEY, space_id TEXT, kind TEXT, title TEXT,
            description TEXT, mime_type TEXT, source_type TEXT, source_uri TEXT, revision TEXT, content_digest TEXT);
        CREATE TABLE knowledge_structured_assets (id TEXT PRIMARY KEY, space_id TEXT, status TEXT,
            file_name TEXT, sheet_name TEXT, source_uri TEXT, content_digest TEXT,
            reference_status TEXT, capabilities TEXT);
    """)
    db.execute("INSERT INTO knowledge_spaces VALUES ('space_tables', 'Tables', '')")
    for row in structured:
        db.execute("INSERT INTO knowledge_structured_assets VALUES (?, ?, 'ready', ?, ?, ?, ?, ?, ?)", row)
    if catalog_asset:
        db.execute("INSERT INTO knowledge_assets VALUES (?, ?, ?, ?, '', ?, 'local', ?, ?, ?)", catalog_asset)
    db.execute("INSERT INTO knowledge_datasets VALUES ('tables', 'space_tables', 'Tables', 'v1', 'logical_dataset', ?, '{}', ?, '[]')", (json.dumps(["table_query"]), json.dumps([item[0] for item in structured])))
    db.commit(); db.close()


def test_snapshot_projects_ready_verified_active_table_assets_with_mime_and_sheet(tmp_path: Path) -> None:
    csv = b"name,total\nA,1\n"
    xlsx = b"xlsx"
    db_path = tmp_path / "catalog.sqlite"
    _database(db_path, structured=[
        ("sales_csv", "space_tables", "sales.csv", None, "remote://ignored", _digest(csv), "ready", '["table_query"]'),
        ("sales_xlsx", "space_tables", "sales.xlsx", "Jan", "remote://ignored", _digest(xlsx), "verified", '["table_query"]'),
        ("sales_active", "space_tables", "sales.tsv", None, "remote://ignored", _digest(b"x"), "active", '["table_query"]'),
    ])
    snapshot = SqliteCatalogQueryRepository(db_path).read_package_snapshot()
    projected = {asset["id"]: asset for asset in snapshot.assets}
    assert projected["sales_csv"]["source_uri"] == "knowledge://spaces/space_tables/assets/sales_csv"
    assert projected["sales_csv"]["source_type"] == "structured_file"
    assert projected["sales_csv"]["mime_type"] == "text/csv"
    assert projected["sales_xlsx"]["mime_type"].endswith("spreadsheetml.sheet")
    assert projected["sales_xlsx"]["sheet_name"] == "Jan"


def test_snapshot_skips_unpublished_or_non_table_structured_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "catalog.sqlite"
    _database(db_path, structured=[("draft", "space_tables", "draft.csv", None, "", _digest(b"x"), "pending", '["table_query"]'), ("other", "space_tables", "other.csv", None, "", _digest(b"x"), "ready", '["document_rag_query"]')])
    ids = {asset["id"] for asset in SqliteCatalogQueryRepository(db_path).read_package_snapshot().assets}
    assert ids == set()


def test_snapshot_rejects_existing_asset_space_or_digest_conflict(tmp_path: Path) -> None:
    content = b"same"
    db_path = tmp_path / "catalog.sqlite"
    _database(db_path, structured=[("same", "space_tables", "same.csv", None, "", _digest(content), "ready", '["table_query"]')], catalog_asset=("same", "space_other", "document", "Same", "text/csv", "knowledge://spaces/space_other/assets/same", "r", _digest(content)))
    with pytest.raises(ValueError, match="conflicts"):
        SqliteCatalogQueryRepository(db_path).read_package_snapshot()


def test_existing_asset_same_digest_preserves_structured_canonical_sheet(tmp_path: Path) -> None:
    content = b"same-xlsx"
    db_path = tmp_path / "catalog.sqlite"
    _database(
        db_path,
        structured=[("same", "space_tables", "sales.xlsx", "Sales", "ignored", _digest(content), "ready", '["table_query"]')],
        catalog_asset=("same", "space_tables", "document", "Sales", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "knowledge://spaces/space_tables/assets/same", "r", _digest(content)),
    )
    snapshot = SqliteCatalogQueryRepository(db_path).read_package_snapshot()
    assert next(asset for asset in snapshot.assets if asset["id"] == "same")["sheet_name"] == "Sales"


@pytest.mark.asyncio
async def test_existing_asset_same_digest_exports_canonical_sheet(tmp_path: Path) -> None:
    content = b"same-xlsx"
    db_path = tmp_path / "catalog.sqlite"
    _database(
        db_path,
        structured=[("same", "space_tables", "sales.xlsx", "Sales", "ignored", _digest(content), "ready", '["table_query"]')],
        catalog_asset=("same", "space_tables", "document", "Sales", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "knowledge://spaces/space_tables/assets/same", "r", _digest(content)),
    )
    source = tmp_path / "sales.xlsx"; source.write_bytes(content)
    destination = Path("/private/tmp") / f"table-package-existing-{tmp_path.name}.zip"
    if destination.exists():
        destination.unlink()
    repository = SqliteCatalogQueryRepository(db_path)
    await PackageExportService(repository, LocalFilesystemBlobReader({"same": source})).export(
        Principal("admin", ("knowledge.admin", "knowledge.space:space_tables")), Correlation("table-existing"),
        output_zip=destination, package_id="tables", version="v1", collections=[{"id": "tables", "version": "v1"}],
    )
    with zipfile.ZipFile(destination) as archive:
        assets = json.loads(archive.read("assets/index.json"))
    assert next(asset for asset in assets["assets"] if asset["id"] == "same")["sheet_name"] == "Sales"


@pytest.mark.asyncio
async def test_real_table_collection_export_reads_projected_csv_and_xlsx(tmp_path: Path) -> None:
    csv = b"name,total\nA,1\n"
    xlsx = b"xlsx-content"
    db_path = tmp_path / "catalog.sqlite"
    _database(db_path, structured=[
        ("sales_csv", "space_tables", "sales.csv", None, "ignored", _digest(csv), "ready", '["table_query"]'),
        ("sales_xlsx", "space_tables", "sales.xlsx", "Jan", "ignored", _digest(xlsx), "active", '["table_query"]'),
    ])
    repository = SqliteCatalogQueryRepository(db_path)
    csv_path = tmp_path / "sales.csv"; csv_path.write_bytes(csv)
    xlsx_path = tmp_path / "sales.xlsx"; xlsx_path.write_bytes(xlsx)
    destination = Path("/private/tmp") / f"table-package-export-{tmp_path.name}.zip"
    if destination.exists():
        destination.unlink()
    await PackageExportService(repository, LocalFilesystemBlobReader({"sales_csv": csv_path, "sales_xlsx": xlsx_path})).export(
        Principal("admin", ("knowledge.admin", "knowledge.space:space_tables")),
        Correlation("table-export"), output_zip=destination, package_id="tables", version="v1",
        collections=[{"id": "tables", "version": "v1"}],
    )
    with zipfile.ZipFile(destination) as archive:
        names = set(archive.namelist())
        assert "assets/originals/sales_csv.csv" in names
        assert "assets/originals/sales_xlsx.xlsx" in names
