from __future__ import annotations

import json
from pathlib import Path

import pytest

from knowledge_platform.distribution.dependency_sbom import DependencySbomError, build_dependency_sbom_shadow


def test_dependency_sbom_shadow_is_deterministic_and_non_releaseable() -> None:
    root = Path(__file__).resolve().parents[2]
    result = build_dependency_sbom_shadow(repo_root=root, source_revision="deadbeef")
    replay = build_dependency_sbom_shadow(repo_root=root, source_revision="deadbeef")
    document = result.to_dict()

    assert result.status == "PHASE10_DEPENDENCY_SBOM_SHADOW_PASS_NOT_ACTIVATABLE"
    assert result.to_dict() == replay.to_dict()
    assert result.python_component_count > 300
    assert result.node_component_count == 4
    assert result.node_manifests_dependency_free is True
    assert result.replay_consistent is True
    assert result.network_contacted is False
    assert result.dependency_install_performed is False
    assert result.independent_repository_verified is False
    assert result.release_artifact_generated is False
    assert result.sbom["bomFormat"] == "CycloneDX"
    assert result.sbom["specVersion"] == "1.5"
    assert len(result.sbom["components"]) == result.python_component_count + result.node_component_count
    assert len(result.sbom["dependencies"]) == len(result.sbom["components"])
    assert all(item["ref"] for item in result.sbom["dependencies"])
    accelerate = next(item for item in result.sbom["components"] if item["name"] == "accelerate")
    assert accelerate["hashes"]
    assert all(item["alg"] == "SHA-256" for item in accelerate["hashes"])
    assert "/Users/" not in json.dumps(document)
    assert "file://" not in json.dumps(document)


def test_dependency_sbom_shadow_report_matches_schema(tmp_path: Path) -> None:
    from jsonschema import validate

    from scripts.phase10_dependency_sbom_shadow import run_shadow

    report = tmp_path / "dependency-sbom.json"
    run_shadow(output_path=report)
    validate(
        json.loads(report.read_text(encoding="utf-8")),
        json.loads(
            (Path(__file__).resolve().parents[2] / "docs/knowledge-platform/phase10-dependency-sbom-shadow.schema.json").read_text()
        ),
    )


def test_dependency_sbom_shadow_rejects_symlinked_lock(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    (tmp_path / "backend").mkdir()
    link = tmp_path / "backend/uv.lock"
    link.symlink_to(root / "backend/uv.lock")
    with pytest.raises(DependencySbomError, match="regular file"):
        # The implementation must inspect the actual repository input, not a
        # caller-controlled path; this also documents the no-symlink contract.
        build_dependency_sbom_shadow(repo_root=tmp_path, source_revision="deadbeef")
