from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from knowledge_platform.distribution.local_catalog_inventory import (
    LocalCatalogInventoryError,
    read_local_catalog_inventory,
)


def _stage(tmp_path: Path, *, assets: int = 2, collections: int = 1) -> Path:
    database = tmp_path / "knowledge-platform.sqlite3"
    connection = sqlite3.connect(database)
    try:
        connection.executescript(
            "CREATE TABLE knowledge_assets (id TEXT PRIMARY KEY);"
            "CREATE TABLE knowledge_datasets (id TEXT PRIMARY KEY);"
        )
        connection.executemany("INSERT INTO knowledge_assets(id) VALUES (?)", ((f"asset-{i}",) for i in range(assets)))
        connection.executemany("INSERT INTO knowledge_datasets(id) VALUES (?)", ((f"dataset-{i}",) for i in range(collections)))
        connection.commit()
    finally:
        connection.close()
    digest = "sha256:" + hashlib.sha256(database.read_bytes()).hexdigest()
    report = tmp_path / "stage.json"
    report.write_text(
        json.dumps(
            {
                "targets": {
                    "platform": {
                        "table_counts": {"knowledge_assets": assets, "knowledge_datasets": collections},
                        "files": {"knowledge-platform.sqlite3": {"bytes": database.stat().st_size, "sha256": digest}},
                        "sha256": digest,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    return report


def test_inventory_reads_real_ids_but_exposes_only_digest_projection(tmp_path: Path) -> None:
    inventory = read_local_catalog_inventory(_stage(tmp_path))

    assert inventory.asset_count == 2
    assert inventory.collection_count == 1
    assert inventory.object_ids == ("asset:asset-0", "asset:asset-1", "collection:dataset-0")
    assert inventory.object_digest.startswith("sha256:")
    assert inventory.database_digest.startswith("sha256:")


def test_inventory_rejects_digest_mismatch(tmp_path: Path) -> None:
    report = _stage(tmp_path)
    document = json.loads(report.read_text(encoding="utf-8"))
    document["targets"]["platform"]["sha256"] = "sha256:" + "0" * 64
    report.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(LocalCatalogInventoryError, match="digests disagree"):
        read_local_catalog_inventory(report)


def test_inventory_rejects_database_symlink(tmp_path: Path) -> None:
    report = _stage(tmp_path)
    database = tmp_path / "knowledge-platform.sqlite3"
    replacement = tmp_path / "replacement.sqlite3"
    replacement.write_bytes(database.read_bytes())
    database.unlink()
    database.symlink_to(replacement)

    with pytest.raises(LocalCatalogInventoryError, match="database is unavailable"):
        read_local_catalog_inventory(report)
