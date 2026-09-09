from __future__ import annotations

import json
from pathlib import Path

import pytest

from knowledge_platform.distribution import PackageArchiveObservation, PackageBuildShadowError, build_package_shadow


def test_platform_packages_build_and_test_in_an_offline_staging_tree() -> None:
    root = Path(__file__).resolve().parents[2]
    result = build_package_shadow(repo_root=root, source_revision="deadbeef")
    document = result.to_dict()
    from jsonschema import validate

    schema = json.loads(
        (root / "docs/knowledge-platform/phase10-package-build-shadow.schema.json").read_text(encoding="utf-8")
    )
    validate(document, schema)
    assert result.status == "PHASE10_PACKAGE_BUILD_SHADOW_PASS_NOT_ACTIVATABLE"
    assert result.replay_consistent is True
    assert document["all_commands_passed"] is True
    assert document["command_count"] == 9
    assert document["archive_manifest_verified"] is True
    assert document["archive_observations"]
    assert all(item["file_count"] > 0 for item in document["archive_observations"])
    assert all(item["bundled_dependency_count"] == 0 for item in document["archive_observations"])
    assert document["network_contacted"] is False
    assert document["independent_repository_verified"] is False
    assert document["release_artifact_generated"] is False
    assert all("/Users/" not in str(item) for item in document["commands"])
    assert all(item.returncode == 0 for item in result.commands)


def test_platform_package_shadow_rejects_symlinked_source(tmp_path: Path) -> None:
    source = tmp_path / "packages" / "knowledge-platform-console"
    source.mkdir(parents=True)
    (source / "package.json").write_text("{}\n", encoding="utf-8")
    (source / "linked").symlink_to(source / "package.json")
    for name, relative in (
        ("console_contracts", "packages/knowledge-platform-console-contracts"),
        ("deploy_cli", "packages/knowledge-platform-deploy-cli"),
        ("skills", "packages/knowledge-platform-skills"),
    ):
        del name
        target = tmp_path / relative
        target.mkdir(parents=True)
        (target / "package.json").write_text("{}\n", encoding="utf-8")
    with pytest.raises(PackageBuildShadowError, match="symlink"):
        build_package_shadow(repo_root=tmp_path, source_revision="deadbeef")


@pytest.mark.parametrize(
    ("name", "files", "bundled"),
    [
        ("../../escape", (("package.json", 1),), 0),
        ("@puddingai/example", (("../escape", 1),), 0),
        ("@puddingai/example", (("package.json", 1),), 1),
    ],
)
def test_package_archive_observation_rejects_unsafe_boundary(
    name: str, files: tuple[tuple[str, int], ...], bundled: int
) -> None:
    with pytest.raises(PackageBuildShadowError):
        PackageArchiveObservation(
            package="console",
            name=name,
            version="0.1.0-local",
            files=files,
            bundled_dependency_count=bundled,
        )
