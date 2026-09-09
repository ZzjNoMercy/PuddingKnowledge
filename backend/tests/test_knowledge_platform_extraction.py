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


def test_phase10_extraction_preflight_rejects_absolute_or_unknown_targets() -> None:
    with pytest.raises(ExtractionPreflightError):
        ExtractionPathPlan("/tmp/source", "manual", "shared-review", "review")
    with pytest.raises(ExtractionPreflightError):
        ExtractionPathPlan(r"C:\source\file.py", "manual", "shared-review", "review")
    with pytest.raises(ExtractionPreflightError):
        ExtractionPathPlan(r"\\server\share\file.py", "manual", "shared-review", "review")
    with pytest.raises(ExtractionPreflightError):
        ExtractionPathPlan("backend/config.py", "extract", "puddingharness", "review")


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
