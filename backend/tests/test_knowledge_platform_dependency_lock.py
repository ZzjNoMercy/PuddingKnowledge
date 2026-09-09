from __future__ import annotations

import json
from pathlib import Path

from knowledge_platform.distribution.dependency_lock import build_dependency_lock_preflight


def test_dependency_lock_preflight_proves_target_lock_coverage_and_remains_non_releaseable() -> None:
    root = Path(__file__).resolve().parents[2]
    result = build_dependency_lock_preflight(repo_root=root)
    document = result.to_dict()
    assert result.status == "PHASE10_DEPENDENCY_LOCK_PREFLIGHT_BLOCKED"
    assert result.replay_consistent is True
    assert result.missing_declared_from_uv_lock == ()
    assert result.missing_requirements_from_uv_lock == ()
    assert result.mixed_project_dependency_graph is False
    assert result.source_project == "puddingknowledge-local"
    assert result.target_lockfiles == (("backend/uv.lock", True),)
    assert result.requirements_packages == ()
    assert document["target_lockfiles_present"] is True
    assert document["network_contacted"] is False
    assert document["lock_regenerated"] is False
    assert document["independent_repository_verified"] is False
    assert all("/Users/" not in json.dumps(item) for item in document["target_lockfiles"])


def test_dependency_lock_preflight_report_matches_schema(tmp_path: Path) -> None:
    from jsonschema import validate

    from scripts.phase10_dependency_lock_preflight import run_preflight

    report_path = tmp_path / "dependency-lock.json"
    run_preflight(output_path=report_path)
    root = Path(__file__).resolve().parents[2]
    validate(
        json.loads(report_path.read_text(encoding="utf-8")),
        json.loads(
            (root / "docs/knowledge-platform/phase10-dependency-lock-preflight.schema.json").read_text(encoding="utf-8")
        ),
    )
