from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from knowledge_platform.distribution import (
    DistributionBoundaryError,
    DistributionBoundaryManifest,
    DistributionPathRule,
    build_phase9_boundary_manifest,
)


def test_phase9_boundary_covers_three_repository_owners_and_current_anchors() -> None:
    manifest = build_phase9_boundary_manifest()
    assert manifest.activation_allowed is False
    assert manifest.target_repositories == ("puddingknowledge", "puddingharness")
    assert {rule.owner for rule in manifest.rules} == {"platform", "harness", "shared"}
    existing = {
        "backend/knowledge_platform/catalog/models.py",
        "backend/knowledge_contracts/query.py",
        "packages/knowledge-platform-skills/manifest.json",
        "packages/knowledge-platform-console-contracts/package.json",
        "packages/knowledge-platform-deploy-cli/package.json",
        "packages/knowledge-platform-console/package.json",
        "packages/knowledge-platform-web/package.json",
        "packages/knowledge-platform-runtime/pyproject.toml",
        "scripts/start-knowledge-local.sh",
        "backend/knowledge/query.py",
        "backend/analytics/nl2sql/training.py",
        "backend/vanna/vanna/base.py",
        "electron/main.js",
        "electron/managers/platform.js",
        "packages/puddingclaw-deploy-cli/src/cli.js",
        "packages/puddingclaw-deploy-cli/src/profile-commands.js",
        "packages/puddingclaw-deploy-cli/src/composition-recipes.js",
        "frontend/src/app/knowledge/page.tsx",
        "frontend/src/app/analytics/page.tsx",
        "backend/config.py",
        "backend/api/knowledge.py",
        "backend/api/mcp.py",
        "frontend/src/lib/api.ts",
        "frontend/src/components/citations/SourcesPanel.tsx",
        "docker-compose.infra.yml",
        "scripts/start-local-infra.sh",
    }
    assert manifest.validate_paths(existing) == ()
    assert manifest.owner_for("backend/knowledge_platform/catalog/models.py").owner == "platform"
    assert manifest.owner_for("scripts/start-knowledge-local.sh").action == "extract"
    assert manifest.owner_for("backend/config.py").action == "manual"
    assert manifest.owner_for("electron/managers/platform.js").action == "manual"
    assert manifest.owner_for("packages/puddingclaw-deploy-cli/src/cli.js").action == "retain"
    assert manifest.owner_for("packages/puddingclaw-deploy-cli/src/profile-commands.js").action == "manual"


def test_phase9_boundary_rejects_unsafe_or_misclassified_rules() -> None:
    with pytest.raises(DistributionBoundaryError):
        DistributionPathRule("../backend/knowledge_platform", "platform", "extract", "unsafe")
    with pytest.raises(DistributionBoundaryError):
        DistributionPathRule("backend/config.py", "platform", "manual", "mixed")


def test_phase9_boundary_rejects_same_specificity_overlap() -> None:
    manifest = DistributionBoundaryManifest(
        format="agent-knowledge-platform-phase9-distribution-boundary/v1",
        phase=9,
        status="PHASE9_DISTRIBUTION_BOUNDARY_INVENTORY_PASS_NOT_ACTIVATABLE",
        activation_allowed=False,
        source_repository="PuddingClaw",
        target_repositories=("puddingknowledge", "puddingharness"),
        rules=(
            DistributionPathRule("foo/*a", "platform", "extract", "platform"),
            DistributionPathRule("foo/a*", "harness", "retain", "harness"),
            DistributionPathRule("bar/*", "shared", "manual", "shared"),
        ),
        unresolved_gates=("manual_review",),
    )
    with pytest.raises(DistributionBoundaryError, match="overlap"):
        manifest.owner_for("foo/aa")


def test_phase9_boundary_shadow_runs_against_current_repository(tmp_path: Path) -> None:
    from scripts.phase9_local_distribution_boundary_shadow import run_shadow

    result = run_shadow(output_dir=tmp_path)
    assert result["status"] == "PHASE9_DISTRIBUTION_BOUNDARY_INVENTORY_PASS_NOT_ACTIVATABLE"
    assert result["activation_allowed"] is False
    assert result["files_moved"] is False
    assert result["files_deleted"] is False
    assert result["missing_anchors"] == []
    assert result["coverage"]["unclassified_count"] == 0
    assert result["coverage"]["ambiguous_count"] == 0
    assert result["console_surface"]["status"] == "verified"
    assert result["console_surface"]["surface_ids"] == [
        "knowledge",
        "analytics",
        "oauth",
        "imports",
        "sources",
        "schema",
        "results",
        "notifications",
    ]
    assert result["console_surface"]["file_count"] == 5


def test_phase9_boundary_shadow_rejects_console_dist_tampering(tmp_path: Path) -> None:
    from scripts.phase9_local_distribution_boundary_shadow import _read_console_surface_manifest

    source_dist = Path(__file__).resolve().parents[2] / "packages/knowledge-platform-console/dist"

    def make_repo(name: str) -> Path:
        repo = tmp_path / name
        package_root = repo / "packages/knowledge-platform-console"
        package_root.mkdir(parents=True)
        shutil.copytree(source_dist, package_root / "dist")
        return repo

    tampered_repo = make_repo("tampered")
    (tampered_repo / "packages/knowledge-platform-console/dist/app.mjs").write_text("tampered\n", encoding="utf-8")
    with pytest.raises(ValueError, match="digest mismatch"):
        _read_console_surface_manifest(tampered_repo)

    extra_repo = make_repo("extra")
    (extra_repo / "packages/knowledge-platform-console/dist/extra.txt").write_text("unexpected\n", encoding="utf-8")
    with pytest.raises(ValueError, match="file set"):
        _read_console_surface_manifest(extra_repo)

    symlink_repo = make_repo("symlink")
    dist = symlink_repo / "packages/knowledge-platform-console/dist"
    (dist / "app.mjs").unlink()
    (dist / "app.mjs").symlink_to(source_dist / "app.mjs")
    with pytest.raises(ValueError, match="regular file"):
        _read_console_surface_manifest(symlink_repo)


def test_phase9_boundary_scoped_audit_reports_unclassified_files() -> None:
    manifest = build_phase9_boundary_manifest()
    audit = manifest.audit_scoped_paths(
        {
            "frontend/src/app/knowledge/page.tsx",
            "frontend/src/unowned/platform-view.tsx",
        },
        ("frontend/src/**",),
    )
    assert audit["scoped_file_count"] == 2
    assert audit["unclassified_paths"] == ("frontend/src/unowned/platform-view.tsx",)
    assert audit["ambiguous_paths"] == ()


def test_phase9_boundary_scoped_audit_accepts_real_unicode_and_route_paths() -> None:
    manifest = build_phase9_boundary_manifest()
    audit = manifest.audit_scoped_paths(
        {
            "frontend/src/app/knowledge/imports/[jobId]/page.tsx",
            "frontend/src/app/knowledge/知识库页面.tsx",
        },
        ("frontend/src/app/knowledge/**",),
    )
    assert audit["scoped_file_count"] == 2
    assert audit["unclassified_paths"] == ()
    assert audit["ambiguous_paths"] == ()


def test_phase9_mixed_surface_marker_shadow_is_manual_review_only(tmp_path: Path) -> None:
    from scripts.phase9_local_mixed_surface_shadow import run_shadow

    result = run_shadow(output_path=tmp_path / "mixed.json")
    assert result["status"] == "PHASE9_MIXED_SURFACE_MARKER_PASS_NOT_ACTIVATABLE"
    assert result["activation_allowed"] is False
    assert result["execution_allowed"] is False
    assert result["manual_review_required"] is True
    assert result["semantic_equivalence_proven"] is False
    assert len(result["probes"]) == 7
    assert all(item["status"] == "verified" for item in result["probes"])
    assert all(item["ownership_matches"] is True for item in result["probes"])
    assert all(all(lines for lines in item["required_marker_lines"]) for item in result["probes"])
    assert all(all(len(lines) <= 8 for lines in item["required_marker_lines"]) for item in result["probes"])
    assert all(len(item["required_marker_line_counts"]) == item["required_marker_count"] for item in result["probes"])
    assert all(len(item["symbol_evidence"]) == item["required_marker_count"] for item in result["probes"])
    assert all(
        evidence["action"] in {"retain_harness", "extract_platform", "shared_adapter"}
        for item in result["probes"]
        for evidence in item["symbol_evidence"]
    )


def test_phase9_mixed_surface_shadow_report_matches_versioned_schema(tmp_path: Path) -> None:
    import json

    from jsonschema import Draft202012Validator

    from scripts.phase9_local_mixed_surface_shadow import _SCHEMA, run_shadow

    report_path = tmp_path / "mixed.json"
    run_shadow(output_path=report_path)
    schema = json.loads(_SCHEMA.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    assert list(Draft202012Validator(schema).iter_errors(json.loads(report_path.read_text(encoding="utf-8")))) == []


def test_phase9_mixed_surface_schema_rejects_invalid_action_missing_evidence_and_long_samples() -> None:
    import copy
    import json

    from jsonschema import Draft202012Validator

    from scripts.phase9_local_mixed_surface_shadow import _SCHEMA

    schema = json.loads(_SCHEMA.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    source = json.loads(
        (Path(__file__).resolve().parents[2] / "artifacts/phase0b-local-catalog/phase9-local-mixed-surface-shadow-report.json").read_text(
            encoding="utf-8"
        )
    )
    invalid_action = copy.deepcopy(source)
    invalid_action["probes"][0]["symbol_evidence"][0]["action"] = "invented_action"
    assert list(validator.iter_errors(invalid_action))

    missing_evidence = copy.deepcopy(source)
    del missing_evidence["probes"][0]["symbol_evidence"]
    assert list(validator.iter_errors(missing_evidence))

    long_samples = copy.deepcopy(source)
    long_samples["probes"][0]["required_marker_lines"][0] = list(range(1, 10))
    assert list(validator.iter_errors(long_samples))


def test_phase9_mixed_surface_marker_shadow_blocks_missing_marker(tmp_path: Path) -> None:
    from scripts.phase9_local_mixed_surface_shadow import run_shadow

    source_root = tmp_path / "repo"
    source = source_root / "frontend/src/lib/api.ts"
    source.parent.mkdir(parents=True)
    source.write_text("const API_BASE = '/api';\n", encoding="utf-8")
    result = run_shadow(repo_root=source_root, output_path=tmp_path / "mixed.json")
    assert result["status"] == "PHASE9_MIXED_SURFACE_MARKER_BLOCKED"
    frontend_probe = next(item for item in result["probes"] if item["probe_id"] == "frontend_api_legacy_boundary")
    assert frontend_probe["missing_marker_count"] == 2


def test_phase9_mixed_surface_marker_shadow_does_not_count_comment_only_markers(tmp_path: Path) -> None:
    from scripts.phase9_local_mixed_surface_shadow import run_shadow

    source_root = tmp_path / "repo"
    source = source_root / "frontend/src/lib/api.ts"
    source.parent.mkdir(parents=True)
    source.write_text(
        "// const API_BASE\n// DIRECT_BACKEND_API_BASE\n// export async function\n",
        encoding="utf-8",
    )
    result = run_shadow(repo_root=source_root, output_path=tmp_path / "mixed.json")
    frontend_probe = next(item for item in result["probes"] if item["probe_id"] == "frontend_api_legacy_boundary")
    assert frontend_probe["status"] == "blocked"
    assert frontend_probe["missing_marker_count"] == 3


def test_phase9_mixed_surface_shadow_blocks_ownership_drift(tmp_path: Path) -> None:
    from knowledge_platform.distribution.mixed_surface import MixedSurfaceProbe, run_mixed_surface_probes

    probe = MixedSurfaceProbe(
        "wrong_owner",
        "frontend/src/lib/api.ts",
        ("const API_BASE",),
        expected_owner="platform",
        expected_action="extract",
    )
    result = run_mixed_surface_probes(repo_root=Path(__file__).resolve().parents[2], probes=(probe,))
    assert result["status"] == "PHASE9_MIXED_SURFACE_MARKER_BLOCKED"
    assert result["probes"][0]["ownership_matches"] is False
