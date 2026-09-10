from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from knowledge_platform.package import (
    KnowledgePackageBuilder,
    PackageBuildError,
    PackageValidationError,
    export_package_zip,
    import_package_zip,
    validate_package,
)


def _digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _build(tmp_path: Path, *, relation_space: str = "space_1", target: str = "asset_normalized") -> Path:
    original = tmp_path / "original.md"
    normalized = tmp_path / "normalized.md"
    original.write_text("original", encoding="utf-8")
    normalized.write_text("normalized", encoding="utf-8")
    root = tmp_path / "package"
    KnowledgePackageBuilder().build(
        output_dir=root,
        package_id="relations",
        version="1",
        spaces=[{"id": "space_1", "name": "Local"}, {"id": "space_2", "name": "Other"}],
        collections=[
            {"id": "collection", "space_id": "space_1", "asset_ids": ["asset_original", "asset_normalized"]},
            {"id": "collection_other", "space_id": "space_2", "asset_ids": []},
        ],
        assets=[
            {
                "id": "asset_original",
                "space_id": "space_1",
                "kind": "document",
                "title": "Original",
                "source_uri": "knowledge://spaces/space_1/assets/asset_original",
                "content_digest": _digest(original),
                "derivatives": {"normalized_markdown": target},
                "published_asset_ids": [target],
            },
            {
                "id": "asset_normalized",
                "space_id": relation_space,
                "kind": "document",
                "title": "Normalized",
                "source_uri": "knowledge://spaces/space_1/assets/asset_normalized",
                "content_digest": _digest(normalized),
                "original_asset_id": "asset_original",
            },
        ],
        asset_files={"asset_original": original, "asset_normalized": normalized},
        capabilities=["document_rag_query"],
        catalog_revision="sha256:" + "a" * 64,
    )
    return root


def test_explicit_asset_relations_survive_build_zip_import(tmp_path: Path) -> None:
    root = _build(tmp_path)
    package = json.loads((root / "assets/index.json").read_text(encoding="utf-8"))
    original = next(item for item in package["assets"] if item["id"] == "asset_original")
    assert original["derivatives"] == {"normalized_markdown": "asset_normalized"}
    assert original["published_asset_ids"] == ["asset_normalized"]
    normalized = next(item for item in package["assets"] if item["id"] == "asset_normalized")
    assert normalized["original_asset_id"] == "asset_original"

    archive = export_package_zip(root, tmp_path / "relations.zip")
    imported = import_package_zip(archive, tmp_path / "imported")
    assert imported.asset_count == 2
    assert validate_package(imported.package_root).package_revision == imported.package_revision


def test_asset_relation_rejects_missing_target(tmp_path: Path) -> None:
    with pytest.raises(PackageBuildError, match="unknown Asset"):
        _build(tmp_path, target="missing")


def test_asset_relation_rejects_cross_space_target(tmp_path: Path) -> None:
    with pytest.raises(PackageBuildError, match="crosses Space"):
        _build(tmp_path, relation_space="space_2")


def test_asset_relation_rejects_self_reference(tmp_path: Path) -> None:
    with pytest.raises(PackageBuildError, match="cannot target itself"):
        _build(tmp_path, target="asset_original")
