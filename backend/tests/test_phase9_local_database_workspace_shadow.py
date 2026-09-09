from __future__ import annotations

import json
import shutil
from pathlib import Path

from knowledge_platform.package import KnowledgePackageBuilder
from scripts.phase9_local_database_workspace_shadow import run_shadow


def _build_database_package(tmp_path: Path) -> Path:
    root = tmp_path / "database-package"
    KnowledgePackageBuilder().build(
        output_dir=root,
        package_id="package_database",
        version="1.0.0",
        spaces=[{"id": "space_1", "name": "Local"}],
        collections=[
            {
                "id": "collection_db",
                "space_id": "space_1",
                "name": "Sales database",
                "version": "v1",
                "kind": "database",
                "asset_ids": [],
                "capabilities": ["database_nl2sql"],
            }
        ],
        assets=[],
        asset_files={},
        capabilities=["database_nl2sql"],
        catalog_revision="sha256:" + "e" * 64,
        database_sources=[
            {
                "id": "source_sales",
                "space_id": "space_1",
                "dataset_id": "collection_db",
                "dialect": "postgresql",
                "ddl": [],
                "documentation": [],
                "sql_examples": [
                    {
                        "id": "sql_sales_total",
                        "question": "sales total",
                        "sql": "SELECT SUM(amount) FROM sales",
                    }
                ],
                "entities": [],
            }
        ],
    )
    return root


def test_local_database_workspace_shadow_is_snapshot_only(tmp_path: Path) -> None:
    report = run_shadow(package_root=_build_database_package(tmp_path), output_dir=tmp_path / "reports")

    assert report["status"] == "PHASE9_LOCAL_DATABASE_WORKSPACE_SHADOW_PASS_NOT_ACTIVATABLE"
    assert report["workspace_validation_status"] == "valid"
    assert report["provider_version"] == "snapshot-package-evidence"
    assert report["live_connection"] is False
    assert report["database_url_contacted"] is False
    assert report["execution_fail_closed"] is True
    saved = json.loads((tmp_path / "reports/phase9-local-database-workspace-shadow.json").read_text())
    serialized = json.dumps(saved, ensure_ascii=False)
    assert '"sql":' not in serialized
    assert "SELECT SUM" not in serialized
    assert "/Users/" not in serialized


def test_local_database_workspace_shadow_rejects_tampered_package(tmp_path: Path) -> None:
    package = _build_database_package(tmp_path)
    tampered = tmp_path / "tampered-package"
    shutil.copytree(package, tampered)
    database_index = tampered / "database/index.json"
    database_index.write_text(
        database_index.read_text(encoding="utf-8").replace("sales total", "tampered query"),
        encoding="utf-8",
    )

    report = run_shadow(package_root=tampered, output_dir=tmp_path / "reports")

    assert report["status"] == "PHASE9_LOCAL_DATABASE_WORKSPACE_SHADOW_BLOCKED"
    assert report["error_type"] == "PackageValidationError"
    assert report["activation_allowed"] is False
