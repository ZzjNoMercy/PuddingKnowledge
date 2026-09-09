from __future__ import annotations

import shutil
import subprocess
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


def test_phase9_boundary_shadow_rejects_console_dist_tampering(tmp_path: Path) -> None:
    from scripts.phase9_local_distribution_boundary_shadow import _read_console_surface_manifest

    repo_root = Path(__file__).resolve().parents[2]
    console_source = repo_root / "packages/knowledge-platform-console"
    contracts_source = repo_root / "packages/knowledge-platform-console-contracts"
    build_root = tmp_path / "console-build"
    build_console = build_root / "packages/knowledge-platform-console"
    build_contracts = build_root / "packages/knowledge-platform-console-contracts"
    shutil.copytree(
        console_source,
        build_console,
        ignore=shutil.ignore_patterns("dist", "node_modules"),
    )
    shutil.copytree(contracts_source, build_contracts)
    node = shutil.which("node")
    assert node is not None, "the boundary test requires the Node.js build tool"
    build = subprocess.run(
        [node, "scripts/build.mjs"],
        cwd=build_console,
        check=False,
        capture_output=True,
        text=True,
    )
    assert build.returncode == 0, build.stderr or build.stdout
    source_dist = build_console / "dist"

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


def test_phase9_mixed_surface_shadow_report_matches_versioned_schema(tmp_path: Path) -> None:
    import json

    from jsonschema import Draft202012Validator

    from scripts.phase9_local_mixed_surface_shadow import _SCHEMA, run_shadow

    report_path = tmp_path / "mixed.json"
    run_shadow(output_path=report_path)
    schema = json.loads(_SCHEMA.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    assert list(Draft202012Validator(schema).iter_errors(json.loads(report_path.read_text(encoding="utf-8")))) == []


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
