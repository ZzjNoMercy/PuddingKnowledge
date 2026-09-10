"""Run the Phase 10/11.20 installation migration state-machine shadow.

The command consumes a digest-verified, read-only object inventory from the
local Catalog stage and, optionally, a verified prepared migration manifest.
It never copies a source Home, opens credentials, changes an active revision,
or starts a production process.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, ValidationError

from knowledge_platform.distribution import (
    CredentialRebind,
    InstallationMigrationManifest,
    MigrationObjectSummary,
    MigrationState,
    replay_partial_target_import_shadow,
    replay_stateful_rollback_shadow,
)
from knowledge_platform.distribution.local_catalog_inventory import read_local_catalog_inventory

_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_STAGE_REPORT = _ROOT / "artifacts/phase0b-local-catalog/local-catalog-stage-report.json"
_DEFAULT_OUTPUT = _ROOT / "artifacts/phase0b-local-catalog/phase10-local-installation-migration-shadow.json"
_MANIFEST_SCHEMA = _ROOT / "docs/knowledge-platform/installation-migration-manifest.schema.json"
_DIGEST = "sha256:" + "0" * 64


def _stable_digest(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _stage_counts(path: Path) -> tuple[int, int]:
    inventory = read_local_catalog_inventory(path)
    return inventory.asset_count, inventory.collection_count


def _stage_object_ids(path: Path, *, stage_assets: int, stage_collections: int) -> tuple[tuple[str, ...], str]:
    inventory = read_local_catalog_inventory(path)
    if (inventory.asset_count, inventory.collection_count) != (stage_assets, stage_collections):
        raise ValueError("local Catalog stage object inventory does not match its counts")
    return inventory.object_ids, inventory.database_digest


def _initial_manifest(*, asset_count: int, collection_count: int) -> InstallationMigrationManifest:
    source_installation_id = _stable_digest(
        {"kind": "puddingclaw-local-shadow", "asset_count": asset_count, "collection_count": collection_count}
    )
    summaries = tuple(
        MigrationObjectSummary(domain, count, _stable_digest({"domain": domain, "count": count}))
        for domain, count in (
            ("session_harness", 0),
            ("knowledge_catalog", asset_count + collection_count),
            ("connector_jobs", 0),
        )
    )
    return InstallationMigrationManifest(
        source_installation_id=source_installation_id,
        source_schema_revision="claw-schema-shadow-v1",
        source_catalog_revision="catalog-shadow-local",
        target_versions=(
            ("puddingharness", "rc-shadow-1"),
            ("puddingknowledge", "rc-shadow-1"),
            ("migration_contract", "v1"),
        ),
        object_summaries=summaries,
        id_resource_mappings=(("catalog-shadow", "knowledge://catalog/shadow"),),
        credential_rebinds=(
            CredentialRebind(
                slot="knowledge-database",
                source_ref_digest=_DIGEST,
                target_ref="credential://shadow/knowledge-database",
                status="pending",
            ),
        ),
        active_writers=tuple((domain, "puddingclaw") for domain in ("session_harness", "knowledge_catalog", "connector_jobs")),
        checkpoint=(("source_snapshot", "not-created-in-shadow"),),
        rollback_strategy="snapshot_restore",
    )


def _manifest_from_document(document: dict[str, Any], *, state: MigrationState) -> InstallationMigrationManifest:
    source = document.get("source")
    targets = document.get("targets")
    summaries = document.get("object_summaries")
    mappings = document.get("id_resource_mappings")
    rebinds = document.get("credential_rebinds")
    writers = document.get("active_writers")
    checkpoint = document.get("checkpoint")
    if not all(isinstance(item, (dict, list, tuple)) for item in (source, targets, summaries, mappings, rebinds, writers, checkpoint)):
        raise ValueError("migration manifest shape is invalid")
    if state is MigrationState.PREPARED and document.get("state") != MigrationState.PREPARED.value:
        raise ValueError("migration manifest must be PREPARED")
    source = source if isinstance(source, dict) else {}
    targets = targets if isinstance(targets, dict) else {}
    summaries = summaries if isinstance(summaries, list) else []
    mappings = mappings if isinstance(mappings, list) else []
    rebinds = rebinds if isinstance(rebinds, list) else []
    writers = writers if isinstance(writers, dict) else {}
    checkpoint = checkpoint if isinstance(checkpoint, dict) else {}
    return InstallationMigrationManifest(
        source_installation_id=str(source.get("installation_id") or ""),
        source_schema_revision=str(source.get("schema_revision") or ""),
        source_catalog_revision=str(source.get("catalog_revision") or ""),
        target_versions=tuple((str(key), str(value)) for key, value in targets.items()),
        object_summaries=tuple(
            MigrationObjectSummary(
                str(item.get("domain") or ""),
                int(item.get("object_count")),
                str(item.get("source_digest") or ""),
            )
            for item in summaries
            if isinstance(item, dict)
        ),
        id_resource_mappings=tuple(
            (str(item.get("source_id") or ""), str(item.get("resource_uri") or ""))
            for item in mappings
            if isinstance(item, dict)
        ),
        credential_rebinds=tuple(
            CredentialRebind(
                slot=str(item.get("slot") or ""),
                source_ref_digest=str(item.get("source_ref_digest") or ""),
                target_ref=str(item.get("target_ref") or ""),
                status=str(item.get("status") or ""),
            )
            for item in rebinds
            if isinstance(item, dict)
        ),
        active_writers=tuple((str(key), str(value)) for key, value in writers.items()),
        checkpoint=tuple((str(key), str(value)) for key, value in checkpoint.items()),
        rollback_strategy=str(document.get("rollback_strategy") or ""),
        state=state,
        snapshot_digest=document.get("snapshot_digest") if state is MigrationState.PREPARED else None,
        staging_namespace=document.get("staging_namespace") if state is MigrationState.PREPARED else None,
        active_installation_revision=None,
        post_cutover_delta_digest=document.get("post_cutover_delta_digest"),
        post_cutover_delta_count=document.get("post_cutover_delta_count", 0),
        rollback_reconciliation_digest=document.get("rollback_reconciliation_digest"),
        rollback_delta_reconciled=document.get("rollback_delta_reconciled", False),
        rollback_window_open=state is MigrationState.PREPARED,
        started_at=str(document.get("started_at") or "local-shadow"),
    )


def _load_prepared_manifest(
    path: Path,
    *,
    stage_assets: int,
    stage_collections: int,
    stage_database_digest: str,
    stage_object_digest: str,
) -> tuple[InstallationMigrationManifest, str]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("prepared migration manifest is not readable") from error
    if not isinstance(document, dict):
        raise ValueError("prepared migration manifest must be an object")
    try:
        schema = json.loads(_MANIFEST_SCHEMA.read_text(encoding="utf-8"))
        Draft202012Validator(schema).validate(document)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValidationError, ValueError) as error:
        raise ValueError("prepared migration manifest schema validation failed") from error
    prepared = _manifest_from_document(document, state=MigrationState.PREPARED)
    catalog_summary = next((item for item in prepared.object_summaries if item.domain == "knowledge_catalog"), None)
    if catalog_summary is None or catalog_summary.object_count != stage_assets + stage_collections:
        raise ValueError("prepared migration manifest does not match local Catalog counts")
    if prepared.source_catalog_revision != stage_database_digest:
        raise ValueError("prepared migration manifest does not match local Catalog database digest")
    if catalog_summary.source_digest != stage_object_digest:
        raise ValueError("prepared migration manifest does not match local Catalog object digest")
    return prepared, _stable_digest(document)


def run_shadow(
    *,
    stage_report: Path = _DEFAULT_STAGE_REPORT,
    output_path: Path = _DEFAULT_OUTPUT,
    migration_manifest: Path | None = None,
) -> dict[str, Any]:
    inventory = read_local_catalog_inventory(stage_report)
    assets, collections = inventory.asset_count, inventory.collection_count
    source_object_ids, stage_database_digest = inventory.object_ids, inventory.database_digest
    prepared_source = None
    prepared_source_digest = None
    if migration_manifest is not None:
        prepared_source, prepared_source_digest = _load_prepared_manifest(
            migration_manifest.expanduser().absolute(),
            stage_assets=assets,
            stage_collections=collections,
            stage_database_digest=stage_database_digest,
            stage_object_digest=inventory.object_digest,
        )
        discovered = replace(
            prepared_source,
            state=MigrationState.DISCOVERED,
            snapshot_digest=None,
            staging_namespace=None,
            active_installation_revision=None,
            post_cutover_delta_digest=None,
            post_cutover_delta_count=0,
            rollback_reconciliation_digest=None,
            rollback_delta_reconciled=False,
            rollback_window_open=False,
            failure_checkpoint=None,
            recovery_evidence_digest=None,
            recovery_count=0,
        )
    else:
        discovered = _initial_manifest(asset_count=assets, collection_count=collections)
    prepared_checkpoint = (
        prepared_source.checkpoint
        if prepared_source is not None
        else (("source_snapshot", "synthetic-only"), ("target_import", "staged-not-activated"))
    )
    if "credential_rebind" not in dict(prepared_checkpoint):
        prepared_checkpoint = (*prepared_checkpoint, ("credential_rebind", "pending"))
    prepared = discovered.transition(
        MigrationState.PREPARED,
        snapshot_digest=prepared_source.snapshot_digest
        if prepared_source is not None
        else _stable_digest({"kind": "synthetic-snapshot", "assets": assets, "collections": collections}),
        staging_namespace=prepared_source.staging_namespace if prepared_source is not None else "shadow-staging",
        checkpoint=prepared_checkpoint,
    )
    partial_import_replay = replay_partial_target_import_shadow(
        source_object_ids=source_object_ids,
        imported_before_failure=source_object_ids[: len(source_object_ids) // 2],
    )
    failed_prepared = prepared.record_failure("target_import")
    recovered_prepared = failed_prepared.recover(
        recovery_evidence_digest=_stable_digest({"kind": "synthetic-recovery", "checkpoint": "target_import"})
    )
    failed_credential_rebind = recovered_prepared.record_credential_rebind_failure("knowledge-database")
    recovered_credential_rebind = failed_credential_rebind.recover_credential_rebind(
        slot="knowledge-database",
        recovery_evidence_digest=_stable_digest({"kind": "synthetic-credential-recovery", "slot": "knowledge-database"}),
    )
    source_snapshot_checkpoint = dict(recovered_credential_rebind.checkpoint).get("source_snapshot", "synthetic-only")
    post_cutover_object_ids = ("post-cutover-asset-00001",)
    rollback_replay = replay_stateful_rollback_shadow(
        source_object_ids=source_object_ids,
        post_cutover_delta=post_cutover_object_ids,
    )
    cutover = recovered_credential_rebind.transition(
        MigrationState.CUTOVER,
        active_installation_revision="shadow-cutover-revision",
        credential_rebinds=tuple(
            CredentialRebind(
                slot=item.slot,
                source_ref_digest=item.source_ref_digest,
                target_ref=item.target_ref,
                status="rebound",
            )
            for item in recovered_credential_rebind.credential_rebinds
        ),
        checkpoint=(
            ("source_snapshot", source_snapshot_checkpoint),
            ("target_import", "verified"),
        ),
    )
    post_cutover = cutover.record_post_cutover_delta(
        delta_digest=_stable_digest({"kind": "synthetic-post-cutover-delta", "object_count": len(post_cutover_object_ids)}),
        object_count=len(post_cutover_object_ids),
    )
    reconciled = post_cutover.reconcile_post_cutover_delta(
        reconciliation_digest=_stable_digest(
            {
                "kind": "synthetic-reverse-delta-reconciliation",
                "delta": post_cutover.post_cutover_delta_digest,
                "source_writer_restored": True,
            }
        )
    )
    rolled_back = reconciled.transition(
        MigrationState.ROLLED_BACK,
        rollback_evidence_digest=_stable_digest({"kind": "synthetic-rollback", "source_writer_restored": True}),
        checkpoint=(
            ("source_snapshot", source_snapshot_checkpoint),
            ("target_import", "verified"),
            ("post_cutover_delta", reconciled.post_cutover_delta_digest),
            ("rollback_reconciliation", reconciled.rollback_reconciliation_digest),
            ("rollback", "source-writer-restored"),
        ),
    )
    replayed = rolled_back.transition(MigrationState.ROLLED_BACK)
    result: dict[str, Any] = {
        "format": "agent-knowledge-platform-phase10-local-installation-migration-shadow/v1",
        "status": "PHASE10_INSTALLATION_MIGRATION_SHADOW_PASS_NOT_ACTIVATABLE",
        "activation_allowed": False,
        "execution_allowed": False,
        "physical_copy_performed": False,
        "secret_bytes_read": False,
        "source_home_changed": False,
        "canonical_catalog_changed": False,
        "replay_idempotent": replayed == rolled_back,
        "failure_recovery_verified": (
            failed_prepared.failure_checkpoint == "target_import"
            and recovered_prepared.failure_checkpoint is None
            and recovered_prepared.recovery_count == 1
            and recovered_prepared.recovery_evidence_digest is not None
            and partial_import_replay.idempotent
        ),
        "partial_target_import_replay_verified": partial_import_replay.idempotent,
        "partial_target_import_replay": partial_import_replay.to_dict(),
        "post_cutover_delta_verified": (
            post_cutover.post_cutover_delta_count == 1
            and post_cutover.post_cutover_delta_digest is not None
        ),
        "credential_rebind_recovery_verified": (
            failed_credential_rebind.failure_checkpoint == "credential_rebind"
            and recovered_credential_rebind.failure_checkpoint is None
            and recovered_credential_rebind.recovery_count == 2
            and recovered_credential_rebind.credential_rebinds[0].status == "rebound"
        ),
        "rollback_reconciliation_verified": (
            reconciled.rollback_delta_reconciled
            and reconciled.rollback_reconciliation_digest is not None
            and rolled_back.rollback_delta_reconciled
        ),
        "stateful_rollback_identity_verified": rollback_replay.identity_preserving,
        "stateful_rollback_replay_verified": rollback_replay.lossless,
        "rollback_evidence_scope": "object_ids_only",
        "stateful_rollback_replay": rollback_replay.to_dict(),
        "failure_recovery_sequence": [
            {"state": "PREPARED", "checkpoint": "target_import", "status": "failed"},
            {"state": "PREPARED", "checkpoint": "target_import", "status": "recovered"},
        ],
        "partial_target_import_replay_sequence": [
            {"state": "PREPARED", "imported_before_failure": partial_import_replay.to_dict()["imported_before_failure_count"]},
            {"state": "PREPARED", "resumed_after_failure": partial_import_replay.to_dict()["resumed_after_failure_count"]},
        ],
        "credential_rebind_recovery_sequence": [
            {"state": "PREPARED", "slot": "knowledge-database", "status": "failed"},
            {"state": "PREPARED", "slot": "knowledge-database", "status": "recovered"},
        ],
        "state_sequence": [item.state.value for item in (discovered, prepared, cutover, rolled_back)],
        "event_sequence": [
            "DISCOVERED",
            "PREPARED",
            "CUTOVER",
            "POST_CUTOVER_DELTA_RECORDED",
            "ROLLBACK_RECONCILIATION_VERIFIED",
            "ROLLED_BACK",
        ],
        "final_manifest": rolled_back.to_dict(),
        "local_stage_counts": {"assets": assets, "collections": collections},
        "source_object_inventory": {
            "asset_count": assets,
            "collection_count": collections,
            "source_object_count": len(source_object_ids),
            "source_object_digest": _stable_digest(source_object_ids),
            "stage_database_digest": stage_database_digest,
        },
        "snapshot": {
            "source_manifest_verified": prepared_source is not None,
            "source_manifest_digest": prepared_source_digest,
            "snapshot_digest": prepared.snapshot_digest,
        },
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    result["report"] = str(output_path)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage-report", type=Path, default=_DEFAULT_STAGE_REPORT)
    parser.add_argument("--output", type=Path, default=_DEFAULT_OUTPUT)
    parser.add_argument("--migration-manifest", type=Path)
    args = parser.parse_args()
    result = run_shadow(
        stage_report=args.stage_report,
        output_path=args.output,
        migration_manifest=args.migration_manifest,
    )
    print(json.dumps({"status": result["status"], "report": result["report"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
