from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from knowledge_platform.distribution import RcValidationError, build_rc_validation_manifest


def _extraction() -> dict[str, object]:
    return {
        "status": "PHASE10_EXTRACTION_PREFLIGHT_NOT_EXECUTABLE",
        "source_revision": "deadbeef",
        "source_tag": None,
        "source_tag_signed": False,
        "source_worktree_clean": False,
        "replay_consistent": True,
    }


def _installation() -> dict[str, object]:
    return {
        "status": "PHASE10_INSTALLATION_MIGRATION_SHADOW_PASS_NOT_ACTIVATABLE",
        "activation_allowed": False,
        "execution_allowed": False,
        "physical_copy_performed": False,
        "secret_bytes_read": False,
        "replay_idempotent": True,
        "failure_recovery_verified": True,
        "partial_target_import_replay_verified": True,
        "post_cutover_delta_verified": True,
        "rollback_reconciliation_verified": True,
        "stateful_rollback_replay_verified": True,
        "credential_rebind_recovery_verified": True,
        "source_home_changed": False,
        "canonical_catalog_changed": False,
        "state_sequence": ["DISCOVERED", "PREPARED", "CUTOVER", "ROLLED_BACK"],
        "final_manifest": {"state": "ROLLED_BACK"},
    }


def test_rc_matrix_is_complete_but_not_releaseable() -> None:
    manifest = build_rc_validation_manifest(extraction_manifest=_extraction(), installation_shadow=_installation())
    document = manifest.to_dict()
    assert manifest.releaseable is False
    assert document["status"] == "PHASE10_RC_PREFLIGHT_NOT_RELEASEABLE"
    assert document["release_rollback_window_open"] is True
    assert [check["name"] for check in document["checks"]] == [
        "platform_independent_build",
        "platform_independent_test",
        "platform_sbom",
        "harness_independent_build",
        "harness_independent_test",
        "harness_sbom",
        "harness_no_knowledge_dependency",
        "external_mcp_e2e",
        "installation_upgrade",
        "failure_recovery",
        "stateful_rollback",
    ]
    assert document["checks"][8]["status"] == "shadow_verified"
    assert document["checks"][9]["status"] == "shadow_verified"
    assert document["checks"][10]["status"] == "shadow_verified"
    assert all(check["status"] == "blocked" for check in document["checks"][:8])


def test_rc_matrix_rejects_wrong_source_or_installation_shadow() -> None:
    with pytest.raises(RcValidationError):
        build_rc_validation_manifest(extraction_manifest={"status": "ready"}, installation_shadow=_installation())
    with pytest.raises(RcValidationError):
        build_rc_validation_manifest(extraction_manifest=_extraction(), installation_shadow={"status": "ready"})
    incomplete_shadow = _installation()
    incomplete_shadow.pop("physical_copy_performed")
    manifest = build_rc_validation_manifest(extraction_manifest=_extraction(), installation_shadow=incomplete_shadow)
    assert manifest.to_dict()["checks"][8]["status"] == "blocked"
    unsafe_source = _extraction()
    unsafe_source["source_revision"] = "/Users/pet/source"
    with pytest.raises(RcValidationError):
        build_rc_validation_manifest(extraction_manifest=unsafe_source, installation_shadow=_installation())


def test_rc_shadow_report_is_path_free_and_matches_schema_shape(tmp_path: Path) -> None:
    from jsonschema import validate

    from scripts.phase10_rc_validation_shadow import run_shadow

    result = run_shadow(output_path=tmp_path / "rc.json")
    schema_path = Path(__file__).resolve().parents[2] / "docs/knowledge-platform/rc-validation-manifest.schema.json"
    validate(json.loads((tmp_path / "rc.json").read_text(encoding="utf-8")), json.loads(schema_path.read_text(encoding="utf-8")))
    serialized = json.dumps(result, ensure_ascii=False)
    assert result["status"] == "PHASE10_RC_PREFLIGHT_NOT_RELEASEABLE"
    assert result["releaseable"] is False
    assert result["source_observations"]["source_inventory"] == "PHASE10_SOURCE_INVENTORY_PREFLIGHT_NOT_RELEASEABLE"
    assert result["source_observations"]["package_build"] == "PHASE10_PACKAGE_BUILD_SHADOW_PASS_NOT_ACTIVATABLE"
    assert result["source_observations"]["dependency_sbom"] == "PHASE10_DEPENDENCY_SBOM_SHADOW_PASS_NOT_ACTIVATABLE"
    assert result["source_observations"]["platform_dependency_closure"] == "PHASE10_PLATFORM_DEPENDENCY_CLOSURE_SHADOW_PASS_NOT_ACTIVATABLE"
    assert result["source_observations"]["dependency_lock"] == "PHASE10_DEPENDENCY_LOCK_PREFLIGHT_BLOCKED"
    assert "/Users/" not in serialized
    assert "file://" not in serialized
    assert "password=" not in serialized
    assert Path(result["report"]).exists()


def test_rc_shadow_can_bind_a_real_local_prepared_manifest(tmp_path: Path) -> None:
    from jsonschema import validate

    from knowledge_platform.distribution.local_catalog_inventory import read_local_catalog_inventory
    from scripts.phase10_local_installation_migration_shadow import _initial_manifest
    from scripts.phase10_rc_validation_shadow import run_shadow

    inventory = read_local_catalog_inventory(Path("artifacts/phase0b-local-catalog/local-catalog-stage-report.json"))
    initial = _initial_manifest(asset_count=45, collection_count=1)
    prepared = replace(
        initial,
        source_catalog_revision=inventory.database_digest,
        object_summaries=tuple(
            replace(item, source_digest=inventory.object_digest)
            if item.domain == "knowledge_catalog"
            else item
            for item in initial.object_summaries
        ),
    ).transition(
        "PREPARED",
        snapshot_digest="sha256:" + "a" * 64,
        staging_namespace="local-shadow-prepared",
        checkpoint=(("source_snapshot", "validated-local-backup"), ("target_import", "not-started")),
    )
    manifest_path = tmp_path / "prepared-manifest.json"
    manifest_path.write_text(json.dumps(prepared.to_dict()), encoding="utf-8")

    result = run_shadow(output_path=tmp_path / "rc-real.json", migration_manifest=manifest_path)

    schema_path = Path(__file__).resolve().parents[2] / "docs/knowledge-platform/rc-validation-manifest.schema.json"
    validate(json.loads((tmp_path / "rc-real.json").read_text(encoding="utf-8")), json.loads(schema_path.read_text(encoding="utf-8")))
    assert result["status"] == "PHASE10_RC_PREFLIGHT_NOT_RELEASEABLE"
    assert result["source_observations"]["installation_migration"] == "real-local-prepared-manifest-state-machine-shadow"
