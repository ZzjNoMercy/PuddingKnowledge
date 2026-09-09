from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from knowledge_platform.distribution import (
    ExtractionPathPlan,
    ExtractionPreflightError,
    build_phase10_extraction_manifest,
)


def test_phase10_extraction_preflight_is_explicitly_non_executable() -> None:
    manifest = build_phase10_extraction_manifest(repo_root=Path(__file__).resolve().parents[2])
    replay = build_phase10_extraction_manifest(repo_root=Path(__file__).resolve().parents[2])

    assert manifest.status == "PHASE10_EXTRACTION_PREFLIGHT_NOT_EXECUTABLE"
    assert manifest.executable is False
    assert manifest.activation_allowed is False
    assert manifest.extraction_method == "git-filter-repo"
    assert manifest.target_repositories == ("puddingknowledge", "puddingharness")
    assert manifest.source_tag_signed is False
    assert manifest.extraction_tool_version is None
    assert dict(manifest.component_versions)["platform_catalog_schema"] == "v12"
    assert dict(manifest.component_versions)["harness_catalog_schema"] == "v1"
    assert dict(manifest.component_versions)["platform_rest_api"] == "v1"
    assert dict(manifest.component_versions)["platform_mcp_protocol"] == "2025-06-18"
    assert dict(manifest.component_versions)["knowledge_package"] == "agent-knowledge-package/v1"
    assert dict(manifest.component_versions)["knowledge_package_sbom"] == "CycloneDX/1.5"
    assert len(dict(manifest.artifact_digests)["extraction_path_plan"]) == 64
    assert len(dict(manifest.artifact_digests)["mixed_file_plan"]) == 64
    assert len(dict(manifest.artifact_digests)["dependency_sbom"]) == 64
    assert "signed_source_tag_missing_or_unverified" in manifest.unresolved_gates
    assert "git_filter_repo_tool_unavailable" in manifest.unresolved_gates
    assert "backend/graph/deepagents_manager.py" in manifest.mixed_file_paths
    assert len(manifest.mixed_file_paths) == 23
    assert len(manifest.mixed_file_plans) == 23
    assert all(plan.action == "manual" and plan.target == "shared-review" for plan in manifest.mixed_file_plans)
    assert all(plan.exclude for plan in manifest.mixed_file_plans)
    router = next(plan for plan in manifest.mixed_file_plans if plan.path.endswith("tool_intent_router.py"))
    assert router.preserve == ()
    assert router.exclude == ("entire ToolIntentRouter middleware",)
    for path in ("backend/knowledge/**", "backend/analytics/**", "backend/vanna/**"):
        plan = next(plan for plan in manifest.path_plans if plan.path == path)
        assert (plan.action, plan.target) == ("preserve-legacy", "PuddingClaw")
    assert any("dependency groups" in plan.rule for plan in manifest.mixed_file_plans)
    assert "backend/graph/middlewares/tool_intent_router.py" in manifest.mixed_file_paths
    assert "backend/prompts/tool_guides/knowledge-retrieval.md" in manifest.mixed_file_paths
    assert "frontend/src/app/analytics/page.tsx" in manifest.mixed_file_paths
    assert manifest.unresolved_gates
    assert not any(item.startswith("path_plan_anchor_missing:") for item in manifest.unresolved_gates)
    assert dict(manifest.phase_gate_statuses)["phase_0a"] == "blocked"
    assert dict(manifest.phase_gate_statuses)["phase_0c"] == "ready"
    assert "phase_0a.golden_baseline_frozen is blocked" in manifest.phase_gate_blockers
    assert "phase_0b.production_catalog_copy_verified is blocked" in manifest.phase_gate_blockers
    assert not any("evidence hash mismatch" in blocker for blocker in manifest.phase_gate_blockers)
    assert "source_worktree_not_clean" in manifest.unresolved_gates
    assert manifest.source_revision
    assert manifest.to_dict() == replay.to_dict()
    assert len(manifest.canonical_digest()) == 64


def test_phase10_extraction_preflight_rejects_absolute_or_unknown_targets() -> None:
    with pytest.raises(ExtractionPreflightError):
        ExtractionPathPlan("/tmp/source", "manual", "shared-review", "review")
    with pytest.raises(ExtractionPreflightError):
        ExtractionPathPlan(r"C:\source\file.py", "manual", "shared-review", "review")
    with pytest.raises(ExtractionPreflightError):
        ExtractionPathPlan(r"\\server\share\file.py", "manual", "shared-review", "review")
    with pytest.raises(ExtractionPreflightError):
        ExtractionPathPlan("backend/config.py", "extract", "puddingharness", "review")


def test_phase10_preflight_report_contains_no_host_paths(tmp_path: Path) -> None:
    from scripts.phase10_extraction_preflight import run_preflight

    output = tmp_path / "preflight.json"
    result = run_preflight(output_path=output)
    payload = output.read_text(encoding="utf-8")

    assert result["status"] == "PHASE10_EXTRACTION_PREFLIGHT_NOT_EXECUTABLE"
    assert '"source_worktree_clean": false' in payload
    assert '"missing_mixed_file_paths": []' in payload
    assert '"phase_gate_statuses"' in payload
    assert '"phase_gate_blockers"' in payload
    assert '"artifact_digests"' in payload
    assert '"mixed_file_plans"' in payload
    assert '"preserve"' in payload
    assert '"exclude"' in payload
    assert '"replay_consistent": true' in payload
    assert '"manifest_digest"' in payload
    assert "/Users/" not in payload
    assert "file://" not in payload


def test_phase10_extraction_manifest_matches_versioned_schema(tmp_path: Path) -> None:
    from jsonschema import validate

    from scripts.phase10_extraction_preflight import run_preflight

    output = tmp_path / "preflight.json"
    run_preflight(output_path=output)
    root = Path(__file__).resolve().parents[2]
    validate(
        json.loads(output.read_text(encoding="utf-8")),
        json.loads(
            (root / "docs/knowledge-platform/phase10-extraction-manifest.schema.json").read_text(encoding="utf-8")
        ),
    )


def test_phase10_extraction_manifest_rejects_unsafe_revision() -> None:
    manifest = build_phase10_extraction_manifest(repo_root=Path(__file__).resolve().parents[2])
    with pytest.raises(ExtractionPreflightError, match="source revision"):
        replace(manifest, source_revision="/Users/pet/source")


def test_mixed_files_override_broad_retained_trees():
    manifest = build_phase10_extraction_manifest(repo_root=Path(__file__).resolve().parents[2])
    for path in ("electron/main.js", "electron/package.json", "packages/puddingclaw-deploy-cli/src/cli.js"):
        plan = manifest.plan_for_path(path)
        assert (plan.action, plan.target) == ("manual", "shared-review")
    for path in ("backend/knowledge/service.py", "backend/vanna/base.py", "frontend/src/app/knowledge/schema/page.tsx"):
        plan = manifest.plan_for_path(path)
        assert (plan.action, plan.target) == ("preserve-legacy", "PuddingClaw")
    with pytest.raises(ExtractionPreflightError):
        manifest.plan_for_path("../unsafe")


def test_new_platform_package_and_contract_anchors_are_platform_owned():
    manifest = build_phase10_extraction_manifest(repo_root=Path(__file__).resolve().parents[2])
    for path in (
        "backend/knowledge_contracts/schemas/query-result.schema.json",
        "packages/knowledge-platform-console-contracts/src/index.mjs",
        "packages/knowledge-platform-runtime/pyproject.toml",
        "packages/knowledge-platform-web/package.json",
    ):
        plan = manifest.plan_for_path(path)
        assert plan is not None
        assert (plan.action, plan.target) == ("extract", "puddingknowledge")
