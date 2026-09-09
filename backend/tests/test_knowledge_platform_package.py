from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from knowledge_platform.database import RecordingVannaIndexRebuilder
from knowledge_platform.package import (
    CatalogPackageSnapshot,
    KnowledgePackageBuilder,
    PackageBuildError,
    PackageExportService,
    PackageImportService,
    PackageValidationError,
    WorkspaceMaterializer,
    export_package_zip,
    import_package_zip,
    validate_package,
)


def test_document_workspace_does_not_require_optional_excel_parser(tmp_path: Path, monkeypatch) -> None:
    import sys

    monkeypatch.setitem(sys.modules, 'pandas', None)
    package_root, revision = _build(tmp_path)
    workspace = WorkspaceMaterializer().materialize(package_root=package_root, workspace_root=tmp_path / 'workspace')
    assert workspace.package_revision == revision
    assert (workspace.workspace_root / 'assets/originals/asset_1.md').read_text() == 'alpha notes'


def _asset_file(tmp_path: Path, content: bytes = b"alpha notes") -> tuple[Path, str]:
    path = tmp_path / "source.md"
    path.write_bytes(content)
    return path, "sha256:" + hashlib.sha256(content).hexdigest()


def _build(tmp_path: Path, content: bytes = b"alpha notes") -> tuple[Path, str]:
    source, digest = _asset_file(tmp_path, content)
    root = tmp_path / "package"
    result = KnowledgePackageBuilder().build(
        output_dir=root,
        package_id="package_local",
        version="1.0.0",
        spaces=[{"id": "space_1", "name": "Local"}],
        collections=[
            {
                "id": "collection_1",
                "space_id": "space_1",
                "name": "Local Collection",
                "version": "v1",
                "kind": "document",
                "asset_ids": ["asset_1"],
                "capabilities": ["knowledge_list", "knowledge_search", "knowledge_read", "knowledge_query"],
                "freshness": {"mode": "snapshot"},
            }
        ],
        assets=[
            {
                "id": "asset_1",
                "space_id": "space_1",
                "kind": "document",
                "title": "Alpha Notes",
                "description": "local fixture",
                "mime_type": "text/markdown",
                "source_type": "local-file",
                "source_uri": "knowledge://spaces/space_1/assets/asset_1",
                "content_digest": digest,
            }
        ],
        asset_files={"asset_1": source},
        capabilities=["knowledge_list", "knowledge_search", "knowledge_read", "knowledge_query"],
        catalog_revision="sha256:" + "e" * 64,
    )
    return root, result.package_revision


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


def _build_csv_package(tmp_path: Path, content: bytes) -> Path:
    source = tmp_path / "table.csv"
    source.write_bytes(content)
    digest = "sha256:" + hashlib.sha256(content).hexdigest()
    root = tmp_path / "table-package"
    KnowledgePackageBuilder().build(
        output_dir=root,
        package_id="package_table",
        version="1.0.0",
        spaces=[{"id": "space_1", "name": "Local"}],
        collections=[
            {
                "id": "dataset_sales",
                "space_id": "space_1",
                "name": "Sales",
                "version": "v1",
                "kind": "logical_dataset",
                "asset_ids": ["table_1"],
                "capabilities": ["table_query"],
            }
        ],
        assets=[
            {
                "id": "table_1",
                "space_id": "space_1",
                "kind": "table",
                "title": "Sales table",
                "source_uri": "knowledge://spaces/space_1/assets/table_1",
                "content_digest": digest,
            }
        ],
        asset_files={"table_1": source},
        capabilities=["table_query"],
        catalog_revision="sha256:" + "e" * 64,
    )
    return root


def _build_xlsx_package(tmp_path: Path) -> Path:
    from openpyxl import Workbook

    source = tmp_path / "table.xlsx"
    workbook = Workbook()
    first = workbook.active
    first.title = "Jan"
    first.append(["brand", "sales"])
    first.append(["Pudding", 12])
    second = workbook.create_sheet("Feb")
    second.append(["brand", "sales"])
    second.append(["Claw", 8])
    workbook.save(source)
    content_digest = "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
    root = tmp_path / "xlsx-package"
    KnowledgePackageBuilder().build(
        output_dir=root,
        package_id="package_xlsx",
        version="1.0.0",
        spaces=[{"id": "space_1", "name": "Local"}],
        collections=[
            {
                "id": "dataset_sales",
                "space_id": "space_1",
                "name": "Sales",
                "version": "v1",
                "kind": "logical_dataset",
                "asset_ids": ["table_1"],
                "capabilities": ["table_query"],
            }
        ],
        assets=[
            {
                "id": "table_1",
                "space_id": "space_1",
                "kind": "spreadsheet",
                "title": "January sales",
                "sheet_name": "Jan",
                "source_uri": "knowledge://spaces/space_1/assets/table_1",
                "content_digest": content_digest,
            }
        ],
        asset_files={"table_1": source},
        capabilities=["table_query"],
        catalog_revision="sha256:" + "e" * 64,
    )
    return root


def _refresh_package_integrity(root: Path) -> None:
    manifest_path = root / "package-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for relative in manifest["files"]:
        path = root / relative
        manifest["files"][relative] = {"bytes": path.stat().st_size, "sha256": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()}
    revision_material = {
        "format": manifest["format"],
        "id": manifest["id"],
        "version": manifest["version"],
        "catalog_revision": manifest["catalog_revision"],
        "capabilities": manifest["capabilities"],
        "files": manifest["files"],
    }
    manifest["package_revision"] = "sha256:" + hashlib.sha256(
        (json.dumps(revision_material, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    ).hexdigest()
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    manifest_path.write_bytes(manifest_bytes)
    checksums = {
        "format": "agent-knowledge-package-checksums/v1",
        "files": {
            **manifest["files"],
            "package-manifest.json": {"bytes": len(manifest_bytes), "sha256": "sha256:" + hashlib.sha256(manifest_bytes).hexdigest()},
        },
    }
    (root / "checksums.json").write_text(json.dumps(checksums, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def test_package_validates_exports_and_materializes_snapshot_workspace(tmp_path: Path) -> None:
    package_root, revision = _build(tmp_path)

    validation = validate_package(package_root)
    zip_path = export_package_zip(package_root, tmp_path / "package.zip")
    imported = import_package_zip(zip_path, tmp_path / "imported-package")
    workspace = WorkspaceMaterializer().materialize(package_root=package_root, workspace_root=tmp_path / "workspace")
    listed = subprocess.run([str(workspace.workspace_root / "bin/knowledge"), "list"], check=True, capture_output=True, text=True)
    validated = subprocess.run([str(workspace.workspace_root / "bin/knowledge"), "validate"], check=True, capture_output=True, text=True)
    searched = subprocess.run(
        [str(workspace.workspace_root / "bin/knowledge"), "search", "--query", "alpha"],
        check=True,
        capture_output=True,
        text=True,
    )
    read = subprocess.run(
        [str(workspace.workspace_root / "bin/knowledge"), "read", "--uri", "knowledge://spaces/space_1/assets/asset_1"],
        check=True,
        capture_output=True,
        text=True,
    )
    queried = subprocess.run(
        [
            str(workspace.workspace_root / "bin/knowledge"),
            "query",
            "--collection",
            "collection_1",
            "--question",
            "alpha",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert validation.package_revision == revision
    assert validation.asset_count == 1
    assert zip_path.is_file()
    assert imported.package_revision == revision
    assert imported.asset_count == 1
    assert workspace.package_revision == revision
    local_binding = workspace.workspace_root / "bindings.local.yaml"
    local_binding_text = local_binding.read_text(encoding="utf-8")
    assert local_binding.is_file()
    assert "not-configured" in local_binding_text
    assert "password" not in local_binding_text.casefold()
    for skill_name in ("knowledge-discovery", "document-rag", "wiki-query", "table-query", "database-query"):
        skill = workspace.workspace_root / "skills" / skill_name / "SKILL.md"
        assert skill.is_file()
        assert "/Users/" not in skill.read_text(encoding="utf-8")
    assert json.loads(listed.stdout)["collections"][0]["id"] == "collection_1"
    package_document = json.loads((package_root / "knowledge-package.yaml").read_text(encoding="utf-8"))
    assert package_document["collections"] == ["./collections/index.json"]
    assert json.loads(validated.stdout) == {"file_count": 22, "package_revision": revision, "status": "valid"}
    assert json.loads(searched.stdout)["count"] == 1
    assert json.loads(read.stdout)["id"] == "asset_1"
    queried_payload = json.loads(queried.stdout)
    assert queried_payload["dataset_version"] == "v1"
    assert queried_payload["package_revision"] == revision
    assert queried_payload["trace_id"].startswith("workspace_knowledge_query_")
    assert queried_payload["results"][0]["asset_id"] == "asset_1"
    assert queried_payload["results"][0]["asset_revision"] == queried_payload["results"][0]["content_digest"]
    assert queried_payload["results"][0]["evidence"][0]["revision"] == queried_payload["results"][0]["asset_revision"]


def test_snapshot_workspace_validate_rejects_extra_file_and_digest_drift(tmp_path: Path) -> None:
    package_root, _ = _build(tmp_path)
    workspace = WorkspaceMaterializer().materialize(package_root=package_root, workspace_root=tmp_path / "workspace")
    extra = workspace.workspace_root / "unexpected.txt"
    extra.write_text("not packaged", encoding="utf-8")

    rejected_extra = subprocess.run(
        [str(workspace.workspace_root / "bin/knowledge"), "validate"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert rejected_extra.returncode != 0
    assert "unlisted" in rejected_extra.stderr
    extra.unlink()

    asset = workspace.workspace_root / "assets/originals/asset_1.md"
    asset.write_bytes(b"changed")
    rejected_digest = subprocess.run(
        [str(workspace.workspace_root / "bin/knowledge"), "validate"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert rejected_digest.returncode != 0
    assert "checksum mismatch" in rejected_digest.stderr


def test_snapshot_workspace_validate_recomputes_package_revision(tmp_path: Path) -> None:
    package_root, _ = _build(tmp_path)
    workspace = WorkspaceMaterializer().materialize(package_root=package_root, workspace_root=tmp_path / "workspace")
    manifest_path = workspace.workspace_root / "package-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["version"] = "tampered"
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    manifest_path.write_bytes(manifest_bytes)
    checksums_path = workspace.workspace_root / "checksums.json"
    checksums = json.loads(checksums_path.read_text(encoding="utf-8"))
    checksums["files"]["package-manifest.json"] = {
        "bytes": len(manifest_bytes),
        "sha256": "sha256:" + hashlib.sha256(manifest_bytes).hexdigest(),
    }
    checksums_path.write_text(json.dumps(checksums, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    rejected = subprocess.run(
        [str(workspace.workspace_root / "bin/knowledge"), "validate"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert rejected.returncode != 0
    assert "package revision is invalid" in rejected.stderr


def test_package_database_evidence_rebuilds_vanna_indexes_deterministically(tmp_path: Path) -> None:
    from knowledge_platform.database import RecordingVannaIndexRebuilder, rebuild_vanna_indexes

    package_root, _ = _build(tmp_path)
    (package_root / "database/index.json").write_text(
        json.dumps(
            {
                "format": "agent-knowledge-database-index/v1",
                "sources": [
                    {
                        "id": "db_sales",
                        "space_id": "space_1",
                        "dataset_id": "collection_1",
                        "dialect": "postgresql",
                        "ddl": [{"id": "ddl_sales", "content": "CREATE TABLE sales (amount integer)"}],
                        "documentation": [{"id": "doc_sales", "content": "Sales amount documentation"}],
                        "sql_examples": [
                            {
                                "id": "sql_sales",
                                "question": "sales total",
                                "sql": "SELECT SUM(amount) FROM sales",
                            }
                        ],
                        "entities": [
                            {
                                "id": "entity_sales",
                                "canonical_name": "sales",
                                "entity_type": "table",
                                "table_column": "sales.amount",
                                "aliases": ["revenue"],
                            }
                        ],
                    }
                ],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    _refresh_package_integrity(package_root)
    rebuilder = RecordingVannaIndexRebuilder()
    result = rebuild_vanna_indexes(package_root=package_root, rebuilder=rebuilder)
    assert result.source_count == 1
    assert result.counts == {"ddl": 1, "documentation": 1, "sql_examples": 1, "entities": 1}
    assert rebuilder.committed is True
    assert rebuilder.items["sql_examples"][0]["sql"] == "SELECT SUM(amount) FROM sales"

    (package_root / "database/index.json").write_text(
        (package_root / "database/index.json").read_text(encoding="utf-8").replace("Sales amount documentation", "token=leak"),
        encoding="utf-8",
    )
    _refresh_package_integrity(package_root)
    with pytest.raises(PackageValidationError):
        rebuild_vanna_indexes(package_root=package_root, rebuilder=RecordingVannaIndexRebuilder())


def test_snapshot_workspace_table_cli_queries_asset_and_logical_dataset(tmp_path: Path) -> None:
    package_root = _build_csv_package(tmp_path, b"brand,sales\nPudding,12\nClaw,8\n")
    workspace = WorkspaceMaterializer().materialize(package_root=package_root, workspace_root=tmp_path / "table-workspace")

    asset_query = subprocess.run(
        [str(workspace.workspace_root / "bin/knowledge"), "table", "query", "--asset-id", "table_1", "--query", "sales"],
        check=True,
        capture_output=True,
        text=True,
    )
    dataset_query = subprocess.run(
        [str(workspace.workspace_root / "bin/knowledge"), "table", "query", "--dataset", "dataset_sales", "--query", "pudding"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(asset_query.stdout)["tables"][0]["row_count"] == 2
    dataset_payload = json.loads(dataset_query.stdout)
    assert dataset_payload["dataset_version"] == "v1"
    assert dataset_payload["package_revision"] == json.loads((package_root / "package-manifest.json").read_text(encoding="utf-8"))["package_revision"]
    assert dataset_payload["trace_id"].startswith("workspace_table_query_")
    assert dataset_payload["tables"][0]["preview_rows"][0]["brand"] == "Pudding"
    assert dataset_payload["tables"][0]["evidence"][0]["revision"] == dataset_payload["tables"][0]["asset_revision"]

    assets_path = workspace.workspace_root / "assets/index.json"
    assets = json.loads(assets_path.read_text(encoding="utf-8"))
    alias = workspace.workspace_root / "assets/originals/alias.csv"
    alias.symlink_to(workspace.workspace_root / "assets/originals/table_1.csv")
    assets["assets"][0]["package_path"] = "assets/originals/alias.csv"
    assets_path.write_text(json.dumps(assets, ensure_ascii=False), encoding="utf-8")
    rejected = subprocess.run(
        [str(workspace.workspace_root / "bin/knowledge"), "table", "query", "--asset-id", "table_1", "--query", "sales"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert rejected.returncode != 0
    assert "checksum mismatch" in rejected.stderr or "symlink" in rejected.stderr


def test_snapshot_workspace_query_requires_dataset_capability(tmp_path: Path) -> None:
    package_root = _build_csv_package(tmp_path, b"brand,sales\nPudding,12\n")
    workspace = WorkspaceMaterializer().materialize(package_root=package_root, workspace_root=tmp_path / "workspace")

    rejected = subprocess.run(
        [
            str(workspace.workspace_root / "bin/knowledge"),
            "query",
            "--dataset",
            "dataset_sales",
            "--question",
            "sales",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert rejected.returncode != 0
    assert "does not expose knowledge_query" in rejected.stderr


def test_snapshot_workspace_query_rejects_secret_bearing_text(tmp_path: Path) -> None:
    package_root, _ = _build(tmp_path, b"alpha token=super-secret-value")
    workspace = WorkspaceMaterializer().materialize(package_root=package_root, workspace_root=tmp_path / "workspace")

    rejected = subprocess.run(
        [
            str(workspace.workspace_root / "bin/knowledge"),
            "query",
            "--dataset",
            "collection_1",
            "--question",
            "alpha",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert rejected.returncode != 0
    assert "secret-bearing" in rejected.stderr


def test_snapshot_workspace_database_nl2sql_matches_evidence_and_execution_is_closed(tmp_path: Path) -> None:
    package_root = _build_database_package(tmp_path)
    workspace = WorkspaceMaterializer().materialize(package_root=package_root, workspace_root=tmp_path / "database-workspace")

    candidate = subprocess.run(
        [
            str(workspace.workspace_root / "bin/knowledge"),
            "database",
            "nl2sql",
            "--dataset",
            "collection_db",
            "--question",
            "sales total",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    result = json.loads(candidate.stdout)
    assert result["status"] == "candidate"
    assert result["query_plan"]["sql"] == "SELECT SUM(amount) FROM sales"
    assert result["query_plan"]["execution_allowed"] is False
    assert result["dataset_version"] == "v1"
    assert result["package_revision"] == json.loads((package_root / "package-manifest.json").read_text(encoding="utf-8"))["package_revision"]
    assert result["trace_id"].startswith("workspace_database_nl2sql_")
    assert result["provenance"] == {
        "space_id": "space_1",
        "dataset_id": "collection_db",
        "dataset_version": "v1",
        "capability": "database_nl2sql",
        "provider_versions": {"nl2sql": "snapshot-package-evidence"},
        "workspace_mode": "snapshot",
        "live_connection": False,
    }
    assert result["warnings"] == ["snapshot_workspace_database_execution_disabled"]
    assert result["evidence"] == [{
        "source_id": "source_sales",
        "example_id": "sql_sales_total",
        "question": "sales total",
    }]

    execution = subprocess.run(
        [
            str(workspace.workspace_root / "bin/knowledge"),
            "database",
            "execute",
            "--query-plan",
            result["query_plan"]["query_plan_id"],
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert execution.returncode != 0
    assert "connected Platform database contract" in execution.stderr


def test_snapshot_workspace_database_nl2sql_does_not_generate_unmatched_sql(tmp_path: Path) -> None:
    package_root = _build_database_package(tmp_path)
    workspace = WorkspaceMaterializer().materialize(package_root=package_root, workspace_root=tmp_path / "database-workspace")

    rejected = subprocess.run(
        [
            str(workspace.workspace_root / "bin/knowledge"),
            "database",
            "nl2sql",
            "--dataset",
            "collection_db",
            "--question",
            "delete every row",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert rejected.returncode != 0
    assert "no matching SQL example" in rejected.stderr


def test_snapshot_workspace_database_nl2sql_rejects_tampered_database_index(tmp_path: Path) -> None:
    package_root = _build_database_package(tmp_path)
    workspace = WorkspaceMaterializer().materialize(package_root=package_root, workspace_root=tmp_path / "database-workspace")
    database_index = workspace.workspace_root / "database/index.json"
    database_index.write_text(database_index.read_text(encoding="utf-8").replace("sales total", "tampered query"), encoding="utf-8")

    rejected = subprocess.run(
        [
            str(workspace.workspace_root / "bin/knowledge"),
            "database",
            "nl2sql",
            "--dataset",
            "collection_db",
            "--question",
            "sales total",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert rejected.returncode != 0
    assert "checksum mismatch" in rejected.stderr


def test_snapshot_workspace_table_cli_queries_xlsx_sheet(tmp_path: Path) -> None:
    package_root = _build_xlsx_package(tmp_path)
    workspace = WorkspaceMaterializer().materialize(package_root=package_root, workspace_root=tmp_path / "xlsx-workspace")

    queried = subprocess.run(
        [str(workspace.workspace_root / "bin/knowledge"), "table", "query", "--asset-id", "table_1", "--query", "pudding"],
        check=True,
        capture_output=True,
        text=True,
    )
    result = json.loads(queried.stdout)
    assert result["tables"][0]["row_count"] == 1
    assert result["tables"][0]["preview_rows"][0]["brand"] == "Pudding"


def test_package_preserves_referenced_semantic_assets_as_markdown(tmp_path: Path) -> None:
    source, digest = _asset_file(tmp_path)
    root = tmp_path / "semantic-package"
    KnowledgePackageBuilder().build(
        output_dir=root,
        package_id="package_semantic",
        version="1.0.0",
        spaces=[{"id": "space_1", "name": "Local"}],
        collections=[
            {
                "id": "collection_1",
                "space_id": "space_1",
                "asset_ids": ["asset_1"],
                "semantic_asset_ids": ["measure:sales"],
                "capabilities": ["knowledge_query"],
            }
        ],
        assets=[
            {
                "id": "asset_1",
                "space_id": "space_1",
                "source_uri": "knowledge://spaces/space_1/assets/asset_1",
                "content_digest": digest,
            }
        ],
        asset_files={"asset_1": source},
        capabilities=["knowledge_query"],
        catalog_revision="sha256:" + "e" * 64,
        semantic_assets=[
            {
                "id": "measure:sales",
                "type": "measure",
                "name": "Sales",
                "body": "# Sales\n\nRevenue measure.",
            }
        ],
    )

    validation = validate_package(root)
    semantic_index = json.loads((root / "semantics/index.json").read_text(encoding="utf-8"))

    assert validation.asset_count == 1
    assert semantic_index["assets"][0]["id"] == "measure:sales"
    assert (root / "semantics/assets/measure:sales.md").is_file()


def test_package_rejects_unresolved_semantic_asset_references(tmp_path: Path) -> None:
    source, digest = _asset_file(tmp_path)
    with pytest.raises(PackageBuildError, match="undeclared Semantic Asset"):
        KnowledgePackageBuilder().build(
            output_dir=tmp_path / "semantic-missing",
            package_id="package_semantic",
            version="1.0.0",
            spaces=[{"id": "space_1", "name": "Local"}],
            collections=[{"id": "collection_1", "space_id": "space_1", "asset_ids": ["asset_1"], "semantic_asset_ids": ["measure:sales"]}],
            assets=[{"id": "asset_1", "space_id": "space_1", "source_uri": "knowledge://spaces/space_1/assets/asset_1", "content_digest": digest}],
            asset_files={"asset_1": source},
            capabilities=["knowledge_query"],
            catalog_revision="sha256:" + "e" * 64,
        )


def test_package_export_fails_closed_on_missing_or_mismatched_asset_file(tmp_path: Path) -> None:
    source, digest = _asset_file(tmp_path)
    kwargs = {
        "output_dir": tmp_path / "package",
        "package_id": "package_local",
        "version": "1.0.0",
        "spaces": [{"id": "space_1", "name": "Local"}],
        "collections": [{"id": "collection_1", "space_id": "space_1", "asset_ids": ["asset_1"]}],
        "assets": [{"id": "asset_1", "space_id": "space_1", "source_uri": "knowledge://spaces/space_1/assets/asset_1", "content_digest": digest}],
        "capabilities": ["knowledge_read"],
        "catalog_revision": "sha256:" + "e" * 64,
    }
    with pytest.raises(PackageBuildError, match="missing asset files"):
        KnowledgePackageBuilder().build(**kwargs, asset_files={})
    with pytest.raises(PackageBuildError, match="content digest"):
        mismatch_kwargs = {
            **kwargs,
            "output_dir": tmp_path / "package-mismatch",
            "assets": [{**kwargs["assets"][0], "content_digest": "sha256:" + "0" * 64}],
        }
        KnowledgePackageBuilder().build(**mismatch_kwargs, asset_files={"asset_1": source})


def test_package_validator_rejects_tampering(tmp_path: Path) -> None:
    package_root, _ = _build(tmp_path)
    asset_path = package_root / "assets/originals/asset_1.md"
    asset_path.write_bytes(b"tampered")

    with pytest.raises(PackageValidationError, match="checksum mismatch"):
        validate_package(package_root)


def test_package_validator_rejects_unlisted_extra_files(tmp_path: Path) -> None:
    package_root, _ = _build(tmp_path)
    (package_root / "credentials.json").write_text('{"token":"must-not-ship"}', encoding="utf-8")

    with pytest.raises(PackageValidationError, match="outside its manifest"):
        validate_package(package_root)


def test_package_validator_rejects_nested_checksum_extra_file(tmp_path: Path) -> None:
    package_root, _ = _build(tmp_path)
    nested = package_root / "extra" / "checksums.json"
    nested.parent.mkdir()
    nested.write_text("{}", encoding="utf-8")

    with pytest.raises(PackageValidationError, match="outside its manifest"):
        validate_package(package_root)


def test_package_validator_rejects_cross_space_collection_asset_reference(tmp_path: Path) -> None:
    package_root, _ = _build(tmp_path)
    spaces_path = package_root / "spaces/index.json"
    spaces = json.loads(spaces_path.read_text(encoding="utf-8"))
    spaces["spaces"].append({"id": "space_2", "name": "Other", "description": ""})
    spaces_path.write_text(json.dumps(spaces, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    assets_path = package_root / "assets/index.json"
    assets = json.loads(assets_path.read_text(encoding="utf-8"))
    assets["assets"][0]["space_id"] = "space_2"
    assets_path.write_text(json.dumps(assets, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _refresh_package_integrity(package_root)

    with pytest.raises(PackageValidationError, match="crosses Space boundary"):
        validate_package(package_root)


def test_package_builder_rejects_host_path_metadata(tmp_path: Path) -> None:
    source, digest = _asset_file(tmp_path)
    with pytest.raises(PackageBuildError, match="host path or secret-bearing"):
        KnowledgePackageBuilder().build(
            output_dir=tmp_path / "unsafe-metadata",
            package_id="package_local",
            version="1.0.0",
            spaces=[{"id": "space_1", "name": "Local", "description": "/Users/pet/private"}],
            collections=[{"id": "collection_1", "space_id": "space_1", "asset_ids": ["asset_1"]}],
            assets=[{"id": "asset_1", "space_id": "space_1", "source_uri": "knowledge://spaces/space_1/assets/asset_1", "content_digest": digest}],
            asset_files={"asset_1": source},
            capabilities=["knowledge_read"],
            catalog_revision="sha256:" + "e" * 64,
        )


def test_package_export_service_binds_catalog_snapshot_provenance(tmp_path: Path) -> None:
    package_root, _ = _build(tmp_path)

    class Source:
        catalog_revision = "sha256:" + "f" * 64

        def read_package_snapshot(self):
            return CatalogPackageSnapshot(
                self.catalog_revision,
                json.loads((package_root / "spaces/index.json").read_text())["spaces"],
                json.loads((package_root / "collections/index.json").read_text())["collections"],
                json.loads((package_root / "assets/index.json").read_text())["assets"],
                [],
                provider_versions={"nl2sql": "catalog-snapshot-v1"},
            )

    source, _ = _asset_file(tmp_path, b"alpha notes")
    output = tmp_path / "catalog-bound-package"
    result = PackageExportService(Source()).export_snapshot(
        output_dir=output,
        package_id="catalog_bound",
        version="1.0.0",
        asset_files={"asset_1": source},
        capabilities=["knowledge_read"],
    )

    assert result.package_revision.startswith("sha256:")
    assert json.loads((output / "package-manifest.json").read_text())["catalog_revision"] == Source.catalog_revision
    assert json.loads((output / "package-manifest.json").read_text())["provider_versions"] == {
        "nl2sql": "catalog-snapshot-v1"
    }


def test_package_import_rejects_traversal_and_symlink_entries(tmp_path: Path) -> None:
    traversal = tmp_path / "traversal.zip"
    symlink = tmp_path / "symlink.zip"
    import zipfile

    with zipfile.ZipFile(traversal, "w") as archive:
        archive.writestr("../escaped.txt", b"no")
    link = zipfile.ZipInfo("link")
    link.create_system = 3
    link.external_attr = 0o120777 << 16
    with zipfile.ZipFile(symlink, "w") as archive:
        archive.writestr(link, b"no")

    with pytest.raises(PackageValidationError, match="safe relative path"):
        import_package_zip(traversal, tmp_path / "traversal-output")
    with pytest.raises(PackageValidationError, match="symlink"):
        import_package_zip(symlink, tmp_path / "symlink-output")


def test_package_import_service_rebuilds_vanna_only_after_validated_import(tmp_path: Path) -> None:
    package_root, _ = _build(tmp_path)
    package_zip = tmp_path / "package.zip"
    export_package_zip(package_root, package_zip)
    rebuilder = RecordingVannaIndexRebuilder()
    result = PackageImportService(rebuilder=rebuilder).import_and_rebuild(
        package_zip=package_zip,
        output_dir=tmp_path / "imported-package",
    )
    assert result.package.package_revision.startswith("sha256:")
    assert result.vanna.source_count == 0
    assert rebuilder.committed is True
