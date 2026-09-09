from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from knowledge_platform.baseline import build_source_snapshot

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT.parent / "docs/knowledge-platform/golden-baseline.yaml"


def test_source_snapshot_covers_every_golden_registry_capability_without_raw_content() -> None:
    snapshot = build_source_snapshot(ROOT.parent, REGISTRY)

    assert snapshot.repository_revision
    assert len(snapshot.capabilities) == 9
    assert {capability.capability_id for capability in snapshot.capabilities} == {
        "document_rag",
        "wiki_query_and_compile",
        "table_query",
        "database_nl2sql_and_readonly_execute",
        "semantic_assets_and_authoring",
        "package_export",
        "connectors_and_capture",
        "lease_and_jobs",
        "catalog_migration_rehearsal",
    }
    payload = snapshot.to_dict()
    assert payload["raw_contents_included"] is False
    assert isinstance(payload["worktree_clean"], bool)
    assert payload["snapshot_digest"].startswith("sha256:")
    assert all("content" not in item for capability in payload["capabilities"] for item in capability["source_files"])
    assert all(capability.source_digest.startswith("sha256:") for capability in snapshot.capabilities)


def test_source_snapshot_is_reproducible_and_rejects_unsafe_or_missing_surfaces(tmp_path: Path) -> None:
    first = build_source_snapshot(ROOT.parent, REGISTRY)
    second = build_source_snapshot(ROOT.parent, REGISTRY)
    assert first.to_dict() == second.to_dict()

    bad_registry = tmp_path / "golden.yaml"
    bad_registry.write_text(
        "format: agent-knowledge-platform-golden-baseline/v1\n"
        "capabilities:\n"
        "  - id: bad\n"
        "    source_surfaces: [../escape]\n"
        "    test_files: [backend/tests/test_phase0a_dependency_inventory.py]\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unsafe surface"):
        build_source_snapshot(ROOT.parent, bad_registry)


def test_checked_in_source_snapshot_matches_the_generator_byte_for_byte() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "backend/scripts/phase0a_source_snapshot.py",
        ],
        cwd=ROOT.parent,
        check=True,
        capture_output=True,
    )
    artifact = ROOT.parent / "docs/knowledge-platform/phase-0a-source-snapshot.json"

    assert result.stdout == artifact.read_bytes()
