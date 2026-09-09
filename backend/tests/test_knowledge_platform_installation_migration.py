from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from knowledge_platform.distribution import (
    CredentialRebind,
    InstallationMigrationError,
    InstallationMigrationManifest,
    MigrationObjectSummary,
    MigrationState,
    replay_partial_target_import_shadow,
    replay_stateful_rollback_shadow,
)

_DIGEST = "sha256:" + "1" * 64


def _manifest() -> InstallationMigrationManifest:
    return InstallationMigrationManifest(
        source_installation_id=_DIGEST,
        source_schema_revision="claw-schema-v1",
        source_catalog_revision="catalog-revision-1",
        target_versions=(("puddingharness", "rc-1"), ("puddingknowledge", "rc-1")),
        object_summaries=tuple(
            MigrationObjectSummary(domain, 1, _DIGEST)
            for domain in ("session_harness", "knowledge_catalog", "connector_jobs")
        ),
        id_resource_mappings=(("asset-1", "knowledge://assets/asset-1"),),
        credential_rebinds=(CredentialRebind("database", _DIGEST, "credential://knowledge/database", "pending"),),
        active_writers=tuple(
            (domain, "puddingclaw") for domain in ("session_harness", "knowledge_catalog", "connector_jobs")
        ),
        checkpoint=(("inventory", "complete"),),
        rollback_strategy="snapshot_restore",
    )


def test_manifest_has_11_20_fields_without_host_paths_or_secret_bytes() -> None:
    document = _manifest().to_dict()
    serialized = json.dumps(document, ensure_ascii=False)
    assert document["format"] == "agent-knowledge-platform-installation-migration/v1"
    assert document["state"] == "DISCOVERED"
    assert document["active_writers"]["knowledge_catalog"] == "puddingclaw"
    assert "installation_id" in document["source"]
    assert "/Users/" not in serialized
    assert "file://" not in serialized
    assert "password=" not in serialized
    assert "secret=" not in serialized
    assert "token=" not in serialized


def test_stateful_rollback_replay_is_lossless_and_rejects_overlap() -> None:
    replay = replay_stateful_rollback_shadow(
        source_object_ids=("asset-00001", "collection-00001"),
        post_cutover_delta=("asset-00002",),
    )
    assert replay.lossless is True
    assert replay.to_dict() == {
        "source_object_count": 2,
        "target_before_rollback_count": 3,
        "post_cutover_delta_count": 1,
        "source_after_rollback_count": 2,
        "target_after_rollback_count": 2,
        "removed_delta_count": 1,
        "active_target_revision_present": False,
        "lossless": True,
    }
    with pytest.raises(InstallationMigrationError, match="overlaps"):
        replay_stateful_rollback_shadow(
            source_object_ids=("asset-00001",),
            post_cutover_delta=("asset-00001",),
        )


def test_partial_target_import_replay_resumes_without_duplicate_objects() -> None:
    replay = replay_partial_target_import_shadow(
        source_object_ids=("asset-00001", "asset-00002", "asset-00003", "collection-00001"),
        imported_before_failure=("asset-00001", "asset-00002"),
    )
    assert replay.idempotent is True
    assert replay.resumed_after_failure == ("asset-00003", "collection-00001")
    with pytest.raises(InstallationMigrationError, match="strict non-empty subset"):
        replay_partial_target_import_shadow(
            source_object_ids=("asset-00001",),
            imported_before_failure=("asset-00001",),
        )


def test_credential_rebind_failure_is_resumable_without_cutover() -> None:
    prepared = _manifest().transition(
        MigrationState.PREPARED,
        snapshot_digest=_DIGEST,
        staging_namespace="staging-1",
        checkpoint=(("inventory", "complete"), ("target_import", "verified"), ("credential_rebind", "pending")),
    )
    failed = prepared.record_credential_rebind_failure("database")
    assert failed.credential_rebinds[0].status == "failed"
    with pytest.raises(InstallationMigrationError, match="credential rebind"):
        failed.transition(
            MigrationState.CUTOVER,
            active_installation_revision="active-revision-2",
        )
    recovered = failed.recover_credential_rebind(slot="database", recovery_evidence_digest=_DIGEST)
    assert recovered.credential_rebinds[0].status == "rebound"
    assert recovered.failure_checkpoint is None
    assert dict(recovered.checkpoint)["credential_rebind"] == "verified"


def test_state_machine_proves_prepared_cutover_and_stateful_rollback() -> None:
    discovered = _manifest()
    prepared = discovered.transition(
        MigrationState.PREPARED,
        snapshot_digest=_DIGEST,
        staging_namespace="staging-1",
        checkpoint=(("inventory", "complete"), ("snapshot", "verified"), ("target_import", "staged")),
    )
    cutover = prepared.transition(
        MigrationState.CUTOVER,
        active_installation_revision="active-revision-2",
        credential_rebinds=(CredentialRebind("database", _DIGEST, "credential://knowledge/database", "rebound"),),
        checkpoint=(("inventory", "complete"), ("snapshot", "verified"), ("target_import", "verified"), ("delta", "captured")),
    )
    rolled_back = cutover.transition(
        MigrationState.ROLLED_BACK,
        rollback_evidence_digest=_DIGEST,
        checkpoint=(
            ("inventory", "complete"),
            ("snapshot", "verified"),
            ("target_import", "verified"),
            ("delta", "captured"),
            ("rollback", "verified"),
        ),
    )
    assert prepared.rollback_window_open is True
    assert dict(cutover.active_writers) == {
        "session_harness": "puddingharness",
        "knowledge_catalog": "puddingknowledge",
        "connector_jobs": "puddingknowledge",
    }
    assert dict(rolled_back.active_writers) == {
        "session_harness": "puddingclaw",
        "knowledge_catalog": "puddingclaw",
        "connector_jobs": "puddingclaw",
    }
    assert rolled_back.state is MigrationState.ROLLED_BACK


def test_post_cutover_delta_requires_reconciliation_before_rollback() -> None:
    prepared = _manifest().transition(
        MigrationState.PREPARED,
        snapshot_digest=_DIGEST,
        staging_namespace="staging-1",
        checkpoint=(("inventory", "complete"), ("target_import", "verified")),
    )
    cutover = prepared.transition(
        MigrationState.CUTOVER,
        active_installation_revision="active-revision-2",
        credential_rebinds=(CredentialRebind("database", _DIGEST, "credential://knowledge/database", "rebound"),),
        checkpoint=(("inventory", "complete"), ("target_import", "verified")),
    )
    delta = cutover.record_post_cutover_delta(delta_digest=_DIGEST, object_count=2)
    with pytest.raises(InstallationMigrationError, match="reconcile"):
        delta.transition(
            MigrationState.ROLLED_BACK,
            rollback_evidence_digest=_DIGEST,
            checkpoint=(("target_import", "verified"), ("post_cutover_delta", _DIGEST), ("rollback", "verified")),
        )
    reconciled = delta.reconcile_post_cutover_delta(reconciliation_digest=_DIGEST)
    rolled_back = reconciled.transition(
        MigrationState.ROLLED_BACK,
        rollback_evidence_digest=_DIGEST,
        checkpoint=(
            ("target_import", "verified"),
            ("post_cutover_delta", _DIGEST),
            ("rollback_reconciliation", _DIGEST),
            ("rollback", "verified"),
        ),
    )
    assert rolled_back.post_cutover_delta_count == 2
    assert rolled_back.rollback_delta_reconciled is True


def test_finalization_closes_only_after_target_writers_and_completion_checkpoint() -> None:
    prepared = _manifest().transition(
        MigrationState.PREPARED,
        snapshot_digest=_DIGEST,
        staging_namespace="staging-1",
        checkpoint=(("inventory", "complete"), ("target_import", "staged")),
    )
    cutover = prepared.transition(
        MigrationState.CUTOVER,
        active_installation_revision="active-revision-2",
        credential_rebinds=(CredentialRebind("database", _DIGEST, "credential://knowledge/database", "rebound"),),
        checkpoint=(("inventory", "complete"), ("target_import", "verified")),
    )
    finalized = cutover.transition(
        MigrationState.FINALIZED,
        completed_at="shadow-complete",
        checkpoint=(("inventory", "complete"), ("target_import", "verified"), ("finalized", "confirmed")),
    )
    assert finalized.rollback_window_open is False
    assert finalized.state is MigrationState.FINALIZED
    assert dict(finalized.active_writers)["knowledge_catalog"] == "puddingknowledge"


def test_replaying_same_state_is_idempotent_and_cannot_mutate_manifest() -> None:
    manifest = _manifest()
    assert manifest.transition(MigrationState.DISCOVERED) is manifest
    with pytest.raises(InstallationMigrationError):
        manifest.transition(MigrationState.DISCOVERED, checkpoint=(("inventory", "changed"),))
    with pytest.raises(InstallationMigrationError):
        manifest.transition(MigrationState.CUTOVER)


def test_cutover_rejects_pending_credential_rebind() -> None:
    prepared = _manifest().transition(
        MigrationState.PREPARED,
        snapshot_digest=_DIGEST,
        staging_namespace="staging-1",
        checkpoint=(("inventory", "complete"), ("target_import", "staged")),
    )
    with pytest.raises(InstallationMigrationError, match="credential rebind"):
        prepared.transition(MigrationState.CUTOVER, active_installation_revision="active-revision-2")


def test_prepared_failure_can_resume_once_and_must_resume_before_cutover() -> None:
    prepared = _manifest().transition(
        MigrationState.PREPARED,
        snapshot_digest=_DIGEST,
        staging_namespace="staging-1",
        checkpoint=(("inventory", "complete"), ("target_import", "staged")),
    )
    failed = prepared.record_failure("target_import")
    assert failed.failure_checkpoint == "target_import"
    recovered = failed.recover(recovery_evidence_digest=_DIGEST)
    assert recovered.failure_checkpoint is None
    assert recovered.recovery_count == 1
    assert recovered.recovery_evidence_digest == _DIGEST
    assert dict(recovered.checkpoint)["target_import"] == "verified"
    with pytest.raises(InstallationMigrationError):
        failed.transition(
            MigrationState.CUTOVER,
            active_installation_revision="active-revision-2",
            credential_rebinds=(CredentialRebind("database", _DIGEST, "credential://knowledge/database", "rebound"),),
        )


def test_manifest_rejects_duplicate_domain_or_unsafe_mapping() -> None:
    with pytest.raises(InstallationMigrationError):
        replace(
            _manifest(),
            object_summaries=_manifest().object_summaries + (_manifest().object_summaries[0],),
        )
    with pytest.raises(InstallationMigrationError):
        replace(
            _manifest(),
            id_resource_mappings=(("asset-1", "/Users/pet/knowledge"),),
        )
    with pytest.raises(InstallationMigrationError):
        CredentialRebind("database", _DIGEST, "credential://knowledge/..", "pending")


def test_schema_declares_versioned_manifest_contract() -> None:
    schema_path = Path(__file__).resolve().parents[2] / "docs/knowledge-platform/installation-migration-manifest.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert schema["$id"].endswith("installation-migration-manifest/v1")
    assert schema["properties"]["format"]["const"] == "agent-knowledge-platform-installation-migration/v1"


def test_local_installation_shadow_uses_current_local_catalog_counts(tmp_path: Path) -> None:
    from scripts.phase10_local_installation_migration_shadow import run_shadow

    result = run_shadow(output_path=tmp_path / "migration-shadow.json")
    assert result["status"] == "PHASE10_INSTALLATION_MIGRATION_SHADOW_PASS_NOT_ACTIVATABLE"
    assert result["local_stage_counts"] == {"assets": 45, "collections": 1}
    assert result["state_sequence"] == ["DISCOVERED", "PREPARED", "CUTOVER", "ROLLED_BACK"]
    assert result["final_manifest"]["state"] == "ROLLED_BACK"
    assert result["replay_idempotent"] is True
    assert result["post_cutover_delta_verified"] is True
    assert result["rollback_reconciliation_verified"] is True
    assert result["stateful_rollback_replay_verified"] is True
    assert result["source_object_inventory"]["source_object_count"] == 46
    assert result["source_object_inventory"]["source_object_digest"].startswith("sha256:")
    assert result["source_object_inventory"]["stage_database_digest"].startswith("sha256:")
    assert result["event_sequence"] == [
        "DISCOVERED",
        "PREPARED",
        "CUTOVER",
        "POST_CUTOVER_DELTA_RECORDED",
        "ROLLBACK_RECONCILIATION_VERIFIED",
        "ROLLED_BACK",
    ]
    assert result["physical_copy_performed"] is False
    assert result["source_home_changed"] is False


def test_local_installation_shadow_inventory_uses_real_staged_catalog_ids() -> None:
    from scripts.phase10_local_installation_migration_shadow import _stage_object_ids

    stage_report = Path("artifacts/phase0b-local-catalog/local-catalog-stage-report.json")
    object_ids, database_digest = _stage_object_ids(stage_report, stage_assets=45, stage_collections=1)

    assert len(object_ids) == 46
    assert object_ids == tuple(sorted(object_ids))
    assert any(object_id.startswith("asset:") for object_id in object_ids)
    assert "collection:dataset_kb_default" in object_ids
    assert database_digest.startswith("sha256:")


def test_local_installation_shadow_can_bind_a_verified_prepared_manifest(tmp_path: Path) -> None:
    from knowledge_platform.distribution.local_catalog_inventory import read_local_catalog_inventory
    from scripts.phase10_local_installation_migration_shadow import _initial_manifest, run_shadow

    stage_report = Path("artifacts/phase0b-local-catalog/local-catalog-stage-report.json")
    inventory = read_local_catalog_inventory(stage_report)
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
        MigrationState.PREPARED,
        snapshot_digest=_DIGEST,
        staging_namespace="local-shadow-prepared",
        checkpoint=(
            ("source_snapshot", "validated-local-backup"),
            ("target_import", "not-started"),
        ),
    )
    manifest_path = tmp_path / "prepared-manifest.json"
    manifest_path.write_text(json.dumps(prepared.to_dict()), encoding="utf-8")

    result = run_shadow(
        migration_manifest=manifest_path,
        output_path=tmp_path / "migration-shadow.json",
    )

    assert result["snapshot"]["source_manifest_verified"] is True
    assert result["snapshot"]["snapshot_digest"] == _DIGEST
    assert result["state_sequence"] == ["DISCOVERED", "PREPARED", "CUTOVER", "ROLLED_BACK"]
    assert result["final_manifest"]["credential_rebinds"][0]["status"] == "rebound"


def test_local_installation_shadow_rejects_manifest_with_unknown_fields(tmp_path: Path) -> None:
    from scripts.phase10_local_installation_migration_shadow import _initial_manifest, run_shadow

    prepared = _initial_manifest(asset_count=45, collection_count=1).transition(
        MigrationState.PREPARED,
        snapshot_digest=_DIGEST,
        staging_namespace="local-shadow-prepared",
        checkpoint=(("source_snapshot", "validated-local-backup"), ("target_import", "not-started")),
    )
    document = prepared.to_dict()
    document["unexpected"] = "must be rejected"
    manifest_path = tmp_path / "prepared-manifest.json"
    manifest_path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ValueError, match="schema validation"):
        run_shadow(migration_manifest=manifest_path, output_path=tmp_path / "migration-shadow.json")


def test_local_installation_shadow_rejects_same_count_with_wrong_catalog_digest(tmp_path: Path) -> None:
    from scripts.phase10_local_installation_migration_shadow import _initial_manifest, run_shadow

    prepared = _initial_manifest(asset_count=45, collection_count=1).transition(
        MigrationState.PREPARED,
        snapshot_digest=_DIGEST,
        staging_namespace="local-shadow-prepared",
        checkpoint=(
            ("source_snapshot", "validated-local-backup"),
            ("target_import", "not-started"),
        ),
    )
    document = prepared.to_dict()
    document["source"]["catalog_revision"] = _DIGEST
    manifest_path = tmp_path / "prepared-manifest.json"
    manifest_path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ValueError, match="database digest"):
        run_shadow(migration_manifest=manifest_path, output_path=tmp_path / "migration-shadow.json")
