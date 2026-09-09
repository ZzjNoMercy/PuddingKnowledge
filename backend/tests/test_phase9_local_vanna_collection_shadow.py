from __future__ import annotations

import json
from pathlib import Path

import pytest

from knowledge_platform.package import KnowledgePackageBuilder
from scripts.phase9_local_vanna_collection_shadow import run_vanna_collection_shadow


def _package(tmp_path: Path) -> Path:
    source = tmp_path / "notes.md"
    source.write_bytes(b"local notes")
    KnowledgePackageBuilder().build(
        output_dir=tmp_path / "package",
        package_id="local-data",
        version="1.0.0",
        spaces=[{"id": "space_1", "name": "Local"}],
        collections=[
            {
                "id": "collection_1",
                "space_id": "space_1",
                "name": "Local Collection",
                "version": "v1",
                "kind": "database",
                "asset_ids": [],
                "capabilities": ["database_nl2sql"],
            }
        ],
        assets=[],
        asset_files={},
        capabilities=["database_nl2sql"],
        catalog_revision="sha256:" + "a" * 64,
        database_sources=[
            {
                "id": "source_1",
                "space_id": "space_1",
                "dataset_id": "collection_1",
                "dialect": "postgresql",
                "ddl": [{"id": "ddl_1", "content": "CREATE TABLE sales (amount integer)"}],
                "documentation": [{"id": "doc_1", "content": "Sales amount"}],
                "sql_examples": [{"id": "sql_1", "question": "sales total", "sql": "SELECT SUM(amount) FROM sales"}],
                "entities": [{
                    "id": "entity_1",
                    "canonical_name": "sales",
                    "entity_type": "table",
                    "table_column": "sales.amount",
                    "aliases": ["revenue"],
                }],
            }
        ],
    )
    return tmp_path / "package"


def test_local_vanna_collection_shadow_rebuilds_package_evidence(tmp_path: Path) -> None:
    report = run_vanna_collection_shadow(
        package_root=_package(tmp_path),
        output_dir=tmp_path / "shadow",
    )

    assert report["status"] == "PHASE9_LOCAL_VANNA_COLLECTION_REBUILD_PASS_NOT_ACTIVATABLE"
    assert report["collection"]["counts"] == {
        "ddl": 1,
        "documentation": 1,
        "entities": 1,
        "sql_examples": 1,
    }
    assert report["collection"]["active"] is False
    assert report["provider_io_performed"] is False
    candidate = next((tmp_path / "shadow" / "collections").iterdir())
    assert json.loads((candidate / "collection-manifest.json").read_text())[
        "activation_allowed"
    ] is False
    assert "CREATE TABLE" in (candidate / "ddl.jsonl").read_text()


def test_local_vanna_collection_shadow_rejects_tampered_package_without_candidate(tmp_path: Path) -> None:
    package = _package(tmp_path)
    manifest = package / "package-manifest.json"
    manifest.write_text(manifest.read_text(encoding="utf-8").replace("local-data", "tampered"), encoding="utf-8")

    report = run_vanna_collection_shadow(package_root=package, output_dir=tmp_path / "shadow")

    assert report["status"] == "PHASE9_LOCAL_VANNA_COLLECTION_REBUILD_FAILED"
    assert report["failure"] == "package_validation_failed"
    assert not (tmp_path / "shadow" / "collections").exists()


def test_local_vanna_collection_shadow_rejects_symlink_output(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    output = tmp_path / "shadow"
    output.symlink_to(target, target_is_directory=True)

    with pytest.raises(ValueError, match="must not be a symlink"):
        run_vanna_collection_shadow(package_root=_package(tmp_path), output_dir=output)
    assert not list(target.iterdir())
