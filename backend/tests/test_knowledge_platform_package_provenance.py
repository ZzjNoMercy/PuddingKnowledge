from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from knowledge_platform.package import (
    KnowledgePackageBuilder,
    PackageBuildError,
    PackageValidationError,
    WorkspaceMaterializer,
    validate_package,
)


def _build_with_provenance(tmp_path: Path, provider_versions: dict[str, str] | None = None) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    source = tmp_path / "asset.md"
    source.write_bytes(b"portable package evidence")
    digest = "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
    output = tmp_path / "package"
    KnowledgePackageBuilder().build(
        output_dir=output,
        package_id="package_provenance",
        version="1.0.0",
        spaces=[{"id": "space_1", "name": "Local"}],
        collections=[{
            "id": "collection_1",
            "space_id": "space_1",
            "name": "Collection",
            "version": "v1",
            "kind": "document",
            "asset_ids": ["asset_1"],
            "capabilities": ["knowledge_query"],
        }],
        assets=[{
            "id": "asset_1",
            "space_id": "space_1",
            "kind": "document",
            "title": "Notes",
            "description": "fixture",
            "mime_type": "text/markdown",
            "source_type": "fixture",
            "source_uri": "knowledge://spaces/space_1/assets/asset_1",
            "content_digest": digest,
        }],
        asset_files={"asset_1": source},
        capabilities=["knowledge_query"],
        catalog_revision="sha256:" + "a" * 64,
        provider_versions=provider_versions or {"embedding": "local-test-v1", "nl2sql": "snapshot-v1"},
    )
    return output


def test_package_exports_reproducible_provider_provenance_sbom(tmp_path: Path) -> None:
    package = _build_with_provenance(tmp_path)
    manifest = json.loads((package / "package-manifest.json").read_text())
    document = json.loads((package / "knowledge-package.yaml").read_text())
    sbom = json.loads((package / "sbom.json").read_text())

    assert manifest["provider_versions"] == {"embedding": "local-test-v1", "nl2sql": "snapshot-v1"}
    assert manifest["sbom"] == "./sbom.json"
    assert document["provider_versions"] == manifest["provider_versions"]
    assert document["sbom"] == {"format": "CycloneDX", "path": "./sbom.json"}
    assert sbom["bomFormat"] == "CycloneDX"
    assert [item["name"] for item in sbom["components"]] == ["embedding", "nl2sql"]
    assert validate_package(package).package_revision == manifest["package_revision"]


def test_package_rejects_secret_or_host_provider_provenance(tmp_path: Path) -> None:
    with pytest.raises(PackageBuildError):
        _build_with_provenance(tmp_path / "secret", {"api_key": "redacted"})
    with pytest.raises(PackageBuildError):
        _build_with_provenance(tmp_path / "path", {"embedding": "/Users/pet/model"})


def test_package_rejects_tampered_provider_sbom(tmp_path: Path) -> None:
    package = _build_with_provenance(tmp_path)
    sbom_path = package / "sbom.json"
    sbom = json.loads(sbom_path.read_text())
    sbom["components"][0]["version"] = "tampered"
    sbom_path.write_text(json.dumps(sbom, indent=2, sort_keys=True) + "\n")
    with pytest.raises(PackageValidationError, match="checksum mismatch"):
        validate_package(package)


def test_workspace_materialization_preserves_provider_provenance_bundle(tmp_path: Path) -> None:
    package = _build_with_provenance(tmp_path)
    workspace = tmp_path / "workspace"

    result = WorkspaceMaterializer().materialize(package_root=package, workspace_root=workspace)
    manifest = json.loads((workspace / "package-manifest.json").read_text())
    checksums = json.loads((workspace / "checksums.json").read_text())

    assert result.package_revision == manifest["package_revision"]
    assert manifest["provider_versions"] == {"embedding": "local-test-v1", "nl2sql": "snapshot-v1"}
    assert manifest["sbom"] == "./sbom.json"
    assert "sbom.json" in checksums["files"]
    validation = subprocess.run(
        [str(workspace / "bin/knowledge"), "validate"],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(validation.stdout)["package_revision"] == result.package_revision
