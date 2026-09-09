from __future__ import annotations

import json
from pathlib import Path

import pytest

from knowledge_platform.distribution.evidence_bundle import EvidenceBundleError, build_evidence_bundle


def test_evidence_bundle_is_deterministic_and_path_free(tmp_path: Path) -> None:
    from tests._knowledge_platform_target_fixtures import write_evidence_reports

    write_evidence_reports(tmp_path)
    result = build_evidence_bundle(repo_root=tmp_path, source_revision="deadbeef")
    replay = build_evidence_bundle(repo_root=tmp_path, source_revision="deadbeef")

    assert result == replay
    assert result["status"] == "LOCAL_EVIDENCE_BUNDLE_PASS_NOT_ACTIVATABLE"
    assert result["report_count"] == 8
    assert result["replay_consistent"] is True
    assert result["network_contacted"] is False
    assert result["source_paths_emitted"] is False
    assert all(item["report_digest"].startswith("sha256:") for item in result["reports"])
    migration = next(item for item in result["reports"] if item["logical_name"] == "phase10_installation_migration")
    assert migration["facts"]["source_manifest_verified"] is True
    assert migration["facts"]["source_manifest_digest"].startswith("sha256:")
    assert migration["facts"]["snapshot_digest"].startswith("sha256:")
    assert "/Users/" not in json.dumps(result)
    assert "file://" not in json.dumps(result)


def test_evidence_bundle_report_matches_schema(tmp_path: Path) -> None:
    from jsonschema import validate

    from scripts.phase10_evidence_bundle import run_shadow
    from tests._knowledge_platform_target_fixtures import write_evidence_reports

    write_evidence_reports(tmp_path)
    output = tmp_path / "evidence-bundle.json"
    run_shadow(repo_root=tmp_path, output_path=output, source_revision="synthetic")
    validate(
        json.loads(output.read_text(encoding="utf-8")),
        json.loads(
            (Path(__file__).resolve().parents[2] / "docs/knowledge-platform/phase10-evidence-bundle.schema.json").read_text()
        ),
    )


def test_evidence_bundle_rejects_missing_report(tmp_path: Path) -> None:
    with pytest.raises(EvidenceBundleError, match="unavailable"):
        build_evidence_bundle(repo_root=tmp_path, source_revision="deadbeef")


def test_evidence_bundle_rejects_unsafe_source_revision() -> None:
    root = Path(__file__).resolve().parents[2]
    with pytest.raises(EvidenceBundleError, match="forbidden marker"):
        build_evidence_bundle(repo_root=root, source_revision="file:///Users/pet/secret")


def test_evidence_bundle_rejects_unverified_source_manifest() -> None:
    document = {"snapshot": {"source_manifest_verified": False}}

    with pytest.raises(EvidenceBundleError, match="verified source manifest"):
        from knowledge_platform.distribution import evidence_bundle

        evidence_bundle._facts("phase10_installation_migration", document)
