from __future__ import annotations

from pathlib import Path

import pytest

from knowledge_platform.distribution import SourceInventoryError, build_source_inventory


def test_source_inventory_is_deterministic_and_non_releaseable() -> None:
    root = Path(__file__).resolve().parents[2]
    inventory = build_source_inventory(repo_root=root, source_revision="deadbeef")
    replay = build_source_inventory(repo_root=root, source_revision="deadbeef")

    assert inventory.status == "PHASE10_SOURCE_INVENTORY_PREFLIGHT_NOT_RELEASEABLE"
    assert inventory.to_dict() == replay.to_dict()
    assert inventory.files
    assert inventory.independent_repository_verified is False
    assert inventory.artifact_generated is False
    assert inventory.sbom_generated is False
    assert inventory.network_contacted is False
    assert inventory.inventory_digest.startswith("sha256:")
    assert all(not item.path.startswith(("/", "~")) for item in inventory.files)


def test_source_inventory_rejects_symlink_root_and_assets(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "file.txt").write_text("safe", encoding="utf-8")
    symlink_root = tmp_path / "symlink-root"
    symlink_root.symlink_to(source, target_is_directory=True)
    with pytest.raises(SourceInventoryError, match="regular directory"):
        build_source_inventory(repo_root=tmp_path, source_revision="deadbeef", roots=(("root", "symlink-root"),))

    asset_link = source / "link.txt"
    asset_link.symlink_to(source / "file.txt")
    with pytest.raises(SourceInventoryError, match="symlink"):
        build_source_inventory(repo_root=tmp_path, source_revision="deadbeef", roots=(("root", "source"),))


def test_source_inventory_does_not_claim_dependency_resolution() -> None:
    root = Path(__file__).resolve().parents[2]
    inventory = build_source_inventory(repo_root=root, source_revision="deadbeef")
    assert inventory.dependency_resolution == "not_attempted"
    assert "release" in inventory.to_dict()["scope"]
