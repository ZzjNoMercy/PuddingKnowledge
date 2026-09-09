from __future__ import annotations

import json
from pathlib import Path

from knowledge_platform.database import DatabaseSchemaTable
from knowledge_platform.package import KnowledgePackageBuilder, validate_package
from scripts.phase9_local_database_vanna_package_shadow import (
    _EXPECTED_COLUMNS,
    _TABLE,
    _collection_file_digests_present,
    _curated_ddl,
    _database_source,
)
from scripts.phase9_local_vanna_collection_shadow import run_vanna_collection_shadow


def _schema() -> DatabaseSchemaTable:
    return DatabaseSchemaTable(
        table_name=_TABLE,
        columns=_EXPECTED_COLUMNS,
        schema_revision="sha256:" + "a" * 64,
    )


def test_local_database_source_uses_observed_schema_and_curated_ddl() -> None:
    source = _database_source(_schema())

    assert source["dataset_id"] == "database_insight_data_vehicle_model_base"
    assert source["ddl"][0]["content"] == _curated_ddl(_EXPECTED_COLUMNS)
    assert len(source["entities"]) == len(_EXPECTED_COLUMNS)
    assert source["sql_examples"][0]["sql"].startswith("SELECT energy_type")


def test_local_database_source_rejects_schema_drift() -> None:
    schema = DatabaseSchemaTable(
        table_name=_TABLE,
        columns=("brand", "energy_type"),
        schema_revision="sha256:" + "a" * 64,
    )

    try:
        _database_source(schema)
    except ValueError as error:
        assert "does not match" in str(error)
    else:  # pragma: no cover - assertion branch
        raise AssertionError("schema drift must be rejected")


def test_database_package_rebuilds_all_local_vanna_categories(tmp_path: Path) -> None:
    source = _database_source(_schema())
    build = KnowledgePackageBuilder().build(
        output_dir=tmp_path / "package",
        package_id="local-database",
        version="schema-v1",
        spaces=[{"id": "space_1", "name": "Local"}],
        collections=[
            {
                "id": "dataset_1",
                "space_id": "space_1",
                "name": "Database",
                "version": "v1",
                "kind": "relational-analytics",
                "asset_ids": [],
                "capabilities": ["database_nl2sql"],
            }
        ],
        assets=[],
        asset_files={},
        capabilities=["database_nl2sql"],
        catalog_revision="sha256:" + "b" * 64,
        database_sources=[{**source, "id": "source_1", "space_id": "space_1", "dataset_id": "dataset_1"}],
    )
    assert validate_package(build.package_root).asset_count == 0

    report = run_vanna_collection_shadow(package_root=build.package_root, output_dir=tmp_path / "vanna")

    assert report["status"] == "PHASE9_LOCAL_VANNA_COLLECTION_REBUILD_PASS_NOT_ACTIVATABLE"
    candidate = next((tmp_path / "vanna" / "collections").iterdir())
    manifest = json.loads((candidate / "collection-manifest.json").read_text(encoding="utf-8"))
    assert manifest["active"] is False
    assert set(manifest["file_digests"]) == {
        "ddl.jsonl",
        "documentation.jsonl",
        "entities.jsonl",
        "sql_examples.jsonl",
    }
    assert _collection_file_digests_present(tmp_path / "vanna") is True
    (candidate / "entities.jsonl").write_text("tampered\n", encoding="utf-8")
    assert _collection_file_digests_present(tmp_path / "vanna") is False


def test_collection_file_digest_replay_rejects_symlink(tmp_path: Path) -> None:
    source = _database_source(_schema())
    build = KnowledgePackageBuilder().build(
        output_dir=tmp_path / "package",
        package_id="local-database",
        version="schema-v1",
        spaces=[{"id": "space_1", "name": "Local"}],
        collections=[
            {
                "id": "dataset_1",
                "space_id": "space_1",
                "name": "Database",
                "version": "v1",
                "kind": "relational-analytics",
                "asset_ids": [],
                "capabilities": ["database_nl2sql"],
            }
        ],
        assets=[],
        asset_files={},
        capabilities=["database_nl2sql"],
        catalog_revision="sha256:" + "c" * 64,
        database_sources=[{**source, "id": "source_1", "space_id": "space_1", "dataset_id": "dataset_1"}],
    )
    run_vanna_collection_shadow(package_root=build.package_root, output_dir=tmp_path / "vanna")
    candidate = next((tmp_path / "vanna" / "collections").iterdir())
    target = tmp_path / "target.jsonl"
    target.write_bytes((candidate / "entities.jsonl").read_bytes())
    (candidate / "entities.jsonl").unlink()
    (candidate / "entities.jsonl").symlink_to(target)

    assert _collection_file_digests_present(tmp_path / "vanna") is False
