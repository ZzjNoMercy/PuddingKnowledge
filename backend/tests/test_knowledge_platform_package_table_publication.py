"""Durable package table publication against the migrated Catalog schema."""
from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from openpyxl import Workbook
from sqlalchemy import create_engine

from knowledge_contracts import Principal
from knowledge_platform.catalog import migrate_to_latest
from knowledge_platform.local.package_import import LocalPackagePublisher
from knowledge_platform.package.builder import KnowledgePackageBuilder, export_package_zip


def _principal() -> Principal:
    return Principal("admin", scopes=("knowledge.admin", "knowledge.space:s"))


def _catalog(path: Path) -> None:
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as c:
        migrate_to_latest(c)
        c.exec_driver_sql("INSERT INTO knowledge_spaces(id,name,description,permissions_json,created_at,updated_at) VALUES ('s','S','','{}','now','now')")
    engine.dispose()


def _package(tmp_path: Path, *, suffix: str, sheet_name: str | None = None, two_tables: bool = False) -> tuple[Path, Path]:
    source = tmp_path / f"source{suffix}"
    if suffix == ".xlsx":
        wb = Workbook(); wb.active.title = "First"; wb.active.append(["wrong"]); wb.active.append(["x"])
        ws = wb.create_sheet("Chosen"); ws.append(["name", "sales"]); ws.append(["Pudding", 3]); wb.save(source)
    else:
        source.write_text("name,sales\nPudding,3\n", encoding="utf-8")
    digest = "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
    assets = [{"id": "table", "space_id": "s", "kind": "spreadsheet" if suffix == ".xlsx" else "table", "title": "Table", "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" if suffix == ".xlsx" else "text/csv", "sheet_name": sheet_name, "source_uri": "knowledge://spaces/s/assets/table", "content_digest": digest}]
    if two_tables:
        other = dict(assets[0]); other["id"] = "table2"; other["source_uri"] = "knowledge://spaces/s/assets/table2"; assets.append(other)
    root = tmp_path / "package"
    KnowledgePackageBuilder().build(output_dir=root, package_id="tables", version="1", spaces=[{"id": "s", "name": "S"}], collections=[{"id": "c", "space_id": "s", "name": "C", "version": "1", "kind": "logical_dataset", "asset_ids": [a["id"] for a in assets], "capabilities": ["table_query"]}], assets=assets, asset_files={a["id"]: source for a in assets}, capabilities=["table_query"], catalog_revision="sha256:" + "a" * 64)
    archive = tmp_path / "package.zip"; export_package_zip(root, archive)
    return archive, source


def test_csv_and_non_first_xlsx_sheet_publish_canonical_profile_and_binding(tmp_path: Path) -> None:
    for suffix, sheet in ((".csv", None), (".xlsx", "Chosen")):
        case_dir = tmp_path / suffix[1:]; case_dir.mkdir()
        archive, _ = _package(case_dir, suffix=suffix, sheet_name=sheet)
        catalog = tmp_path / f"{suffix[1:]}.db"; _catalog(catalog)
        with LocalPackagePublisher(catalog, tmp_path / f"state-{suffix[1:]}") as publisher:
            result = asyncio.run(publisher.import_package(_principal(), archive, "sha256:" + hashlib.sha256(archive.read_bytes()).hexdigest()))
            assert result["idempotent"] is False
            with sqlite3.connect(catalog) as db:
                row = db.execute("SELECT source_type,document_asset_id,profile_status,row_count,column_count,columns_json,sheet_name,metadata_json FROM knowledge_structured_assets WHERE id='table'").fetchone()
                assert row[:7] == ("package", "table", "ready", 1, 2, '["name","sales"]', sheet)
                assert json.loads(db.execute("SELECT binding_json FROM knowledge_collection_bindings WHERE capability='table_query'").fetchone()[0]) == {"asset_id": "table"}


def test_table_package_rejects_ambiguous_selection_and_rolls_back(tmp_path: Path) -> None:
    archive, _ = _package(tmp_path, suffix=".csv", two_tables=True)
    catalog = tmp_path / "catalog.db"; _catalog(catalog)
    with LocalPackagePublisher(catalog, tmp_path / "state") as publisher:
        with pytest.raises(ValueError, match="exactly one"):
            asyncio.run(publisher.import_package(_principal(), archive, "sha256:" + hashlib.sha256(archive.read_bytes()).hexdigest()))
    with sqlite3.connect(catalog) as db:
        assert db.execute("SELECT COUNT(*) FROM knowledge_assets").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM knowledge_structured_assets").fetchone()[0] == 0


def test_table_replay_rejects_tampered_publication_metadata(tmp_path: Path) -> None:
    archive, _ = _package(tmp_path, suffix=".csv")
    catalog = tmp_path / "catalog.db"; _catalog(catalog)
    digest = "sha256:" + hashlib.sha256(archive.read_bytes()).hexdigest()
    with LocalPackagePublisher(catalog, tmp_path / "state") as publisher:
        asyncio.run(publisher.import_package(_principal(), archive, digest))
        with sqlite3.connect(catalog) as db:
            row = json.loads(db.execute("SELECT metadata_json FROM knowledge_structured_assets WHERE id='table'").fetchone()[0])
            row["package_revision"] = "sha256:" + "f" * 64
            db.execute("UPDATE knowledge_structured_assets SET metadata_json=? WHERE id='table'", (json.dumps(row),))
            db.commit()
        with pytest.raises(ValueError, match="structured profile publication changed"):
            asyncio.run(publisher.import_package(_principal(), archive, digest))
