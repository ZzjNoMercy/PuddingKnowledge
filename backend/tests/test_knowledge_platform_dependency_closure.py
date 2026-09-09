from __future__ import annotations

import json
from pathlib import Path

import pytest

from knowledge_platform.distribution.dependency_closure import build_dependency_closure_shadow


def test_platform_dependency_closure_is_static_replayable_and_clean() -> None:
    root = Path(__file__).resolve().parents[2]
    result = build_dependency_closure_shadow(repo_root=root)
    document = result.to_dict()
    assert result.status == "PHASE10_PLATFORM_DEPENDENCY_CLOSURE_SHADOW_PASS_NOT_ACTIVATABLE"
    assert result.replay_consistent is True
    assert result.findings == ()
    assert result.scanned_file_count > 100
    assert result.import_edge_count >= result.local_edge_count
    assert document["dependency_resolution"] == "static_imports_only"
    assert document["network_contacted"] is False
    assert document["independent_repository_verified"] is False
    assert document["runtime_dependencies_verified"] is False
    assert all("/Users/" not in json.dumps(item) for item in document["external_import_roots"])


def test_dependency_closure_reports_forbidden_and_unresolved_imports(tmp_path: Path) -> None:
    contracts = tmp_path / "backend/knowledge_contracts"
    platform = tmp_path / "backend/knowledge_platform"
    contracts.mkdir(parents=True)
    platform.mkdir(parents=True)
    (contracts / "__init__.py").write_text("\n", encoding="utf-8")
    (platform / "__init__.py").write_text("\n", encoding="utf-8")
    (platform / "bad.py").write_text("import graph\nimport knowledge_platform.missing\nimport scripts.rehearsal\n", encoding="utf-8")

    result = build_dependency_closure_shadow(repo_root=tmp_path)
    assert result.status == "PHASE10_PLATFORM_DEPENDENCY_CLOSURE_SHADOW_BLOCKED"
    assert [(item.rule_id, item.target) for item in result.findings] == [
        ("forbidden_import", "graph"),
        ("unresolved_local_import", "knowledge_platform.missing"),
        ("forbidden_import", "scripts.rehearsal"),
    ]


def test_dependency_closure_rejects_symlinked_source(tmp_path: Path) -> None:
    contracts = tmp_path / "backend/knowledge_contracts"
    platform = tmp_path / "backend/knowledge_platform"
    contracts.mkdir(parents=True)
    platform.mkdir(parents=True)
    (contracts / "__init__.py").write_text("\n", encoding="utf-8")
    (platform / "__init__.py").write_text("\n", encoding="utf-8")
    (platform / "target.py").write_text("\n", encoding="utf-8")
    (platform / "link.py").symlink_to(platform / "target.py")
    with pytest.raises(ValueError, match="symlink"):
        build_dependency_closure_shadow(repo_root=tmp_path)


def test_dependency_closure_shadow_report_matches_schema(tmp_path: Path) -> None:
    from jsonschema import validate

    from scripts.phase10_platform_dependency_closure_shadow import run_shadow

    report_path = tmp_path / "dependency.json"
    run_shadow(output_path=report_path)
    schema_root = Path(__file__).resolve().parents[2] / "docs/knowledge-platform"
    validate(
        json.loads(report_path.read_text(encoding="utf-8")),
        json.loads((schema_root / "phase10-platform-dependency-closure-shadow.schema.json").read_text(encoding="utf-8")),
    )
