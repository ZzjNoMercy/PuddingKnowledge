"""Provider-neutral installation migration contract for Phase 10 shadows.

This module models the state machine from specification section 11.20.  It is
deliberately an in-memory, side-effect-free contract: a production installer
and a Platform migration API must provide the durable snapshot, active
revision switch, and credential provider behind this boundary.  No method in
this module reads a host path, secret, or PuddingClaw runtime singleton.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any


class InstallationMigrationError(ValueError):
    """Raised when a manifest or state transition violates the contract."""


class MigrationState(StrEnum):
    DISCOVERED = "DISCOVERED"
    PREPARED = "PREPARED"
    CUTOVER = "CUTOVER"
    ROLLED_BACK = "ROLLED_BACK"
    FINALIZED = "FINALIZED"


MIGRATION_DOMAINS = ("session_harness", "knowledge_catalog", "connector_jobs")
SOURCE_WRITER = "puddingclaw"
TARGET_WRITERS = {"session_harness": "puddingharness", "knowledge_catalog": "puddingknowledge", "connector_jobs": "puddingknowledge"}
_FORMAT = "agent-knowledge-platform-installation-migration/v1"
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,159}$")
_SAFE_CHECKPOINT_KEY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_DISALLOWED_TEXT = ("/Users/", "/private/", "file://", "password=", "secret=", "token=")


def _object_ids(values: tuple[str, ...], *, label: str) -> tuple[str, ...]:
    if not isinstance(values, tuple) or any(not isinstance(value, str) for value in values):
        raise InstallationMigrationError(f"{label} must be a tuple of object IDs")
    normalized = tuple(sorted(values))
    if normalized != values or len(set(values)) != len(values):
        raise InstallationMigrationError(f"{label} must be sorted and unique")
    for value in values:
        _safe_token(value, label=f"{label} object ID")
    return values


@dataclass(frozen=True, slots=True)
class StatefulRollbackReplay:
    """Verify a lossless reverse-delta replay over opaque object identities."""

    source_before: tuple[str, ...]
    target_before_rollback: tuple[str, ...]
    post_cutover_delta: tuple[str, ...]
    source_after_rollback: tuple[str, ...]
    target_after_rollback: tuple[str, ...]
    removed_delta: tuple[str, ...]
    active_target_revision_present: bool = False

    def __post_init__(self) -> None:
        for label, values in (
            ("source-before", self.source_before),
            ("target-before-rollback", self.target_before_rollback),
            ("post-cutover delta", self.post_cutover_delta),
            ("source-after-rollback", self.source_after_rollback),
            ("target-after-rollback", self.target_after_rollback),
            ("removed delta", self.removed_delta),
        ):
            _object_ids(values, label=label)
        if set(self.source_before) & set(self.post_cutover_delta):
            raise InstallationMigrationError("post-cutover delta overlaps the source snapshot")
        if self.target_before_rollback != tuple(sorted((*self.source_before, *self.post_cutover_delta))):
            raise InstallationMigrationError("target rollback input does not equal source plus delta")
        if self.source_after_rollback != self.source_before:
            raise InstallationMigrationError("rollback did not restore the source snapshot")
        if self.target_after_rollback != self.source_before:
            raise InstallationMigrationError("rollback did not remove the post-cutover delta")
        if self.removed_delta != self.post_cutover_delta:
            raise InstallationMigrationError("rollback removed an unexpected delta")
        if not isinstance(self.active_target_revision_present, bool):
            raise InstallationMigrationError("active target revision state must be boolean")
        if self.active_target_revision_present:
            raise InstallationMigrationError("rollback replay cannot retain an active target revision")

    @property
    def lossless(self) -> bool:
        return (
            self.source_after_rollback == self.source_before
            and self.removed_delta == self.post_cutover_delta
            and self.target_after_rollback == self.source_before
            and self.active_target_revision_present is False
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_object_count": len(self.source_before),
            "target_before_rollback_count": len(self.target_before_rollback),
            "post_cutover_delta_count": len(self.post_cutover_delta),
            "source_after_rollback_count": len(self.source_after_rollback),
            "target_after_rollback_count": len(self.target_after_rollback),
            "removed_delta_count": len(self.removed_delta),
            "active_target_revision_present": self.active_target_revision_present,
            "lossless": self.lossless,
        }


@dataclass(frozen=True, slots=True)
class PartialTargetImportReplay:
    """Verify resumable target import after a partial first attempt."""

    source_object_ids: tuple[str, ...]
    imported_before_failure: tuple[str, ...]
    resumed_after_failure: tuple[str, ...]
    final_target_object_ids: tuple[str, ...]
    duplicate_import_count: int = 0

    def __post_init__(self) -> None:
        for label, values in (
            ("source snapshot", self.source_object_ids),
            ("partial target import", self.imported_before_failure),
            ("resumed target import", self.resumed_after_failure),
            ("final target import", self.final_target_object_ids),
        ):
            _object_ids(values, label=label)
        source = set(self.source_object_ids)
        imported = set(self.imported_before_failure)
        resumed = set(self.resumed_after_failure)
        if not imported or len(imported) >= len(source) or not imported < source:
            raise InstallationMigrationError("first target import must be a strict non-empty subset")
        if not imported <= source or not resumed <= source or imported & resumed:
            raise InstallationMigrationError("partial target import sets are inconsistent")
        if self.resumed_after_failure != tuple(sorted(source - imported)):
            raise InstallationMigrationError("resume target import does not contain exactly the remaining objects")
        if self.final_target_object_ids != self.source_object_ids:
            raise InstallationMigrationError("resumed target import did not restore the source object set")
        if not isinstance(self.duplicate_import_count, int) or self.duplicate_import_count != 0:
            raise InstallationMigrationError("partial target import contains duplicates")

    @property
    def idempotent(self) -> bool:
        return (
            set(self.imported_before_failure) | set(self.resumed_after_failure) == set(self.source_object_ids)
            and len(self.imported_before_failure) + len(self.resumed_after_failure) == len(self.source_object_ids)
            and self.duplicate_import_count == 0
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_object_count": len(self.source_object_ids),
            "imported_before_failure_count": len(self.imported_before_failure),
            "resumed_after_failure_count": len(self.resumed_after_failure),
            "final_target_object_count": len(self.final_target_object_ids),
            "duplicate_import_count": self.duplicate_import_count,
            "idempotent": self.idempotent,
        }


def replay_stateful_rollback_shadow(
    *, source_object_ids: tuple[str, ...], post_cutover_delta: tuple[str, ...]
) -> StatefulRollbackReplay:
    """Perform a deterministic, path-free reverse-delta replay in memory."""

    source = _object_ids(source_object_ids, label="source snapshot")
    delta = _object_ids(post_cutover_delta, label="post-cutover delta")
    if not delta:
        raise InstallationMigrationError("stateful rollback replay requires a non-empty delta")
    if set(source) & set(delta):
        raise InstallationMigrationError("post-cutover delta overlaps the source snapshot")
    target_before = tuple(sorted((*source, *delta)))
    return StatefulRollbackReplay(
        source_before=source,
        target_before_rollback=target_before,
        post_cutover_delta=delta,
        source_after_rollback=source,
        target_after_rollback=source,
        removed_delta=delta,
    )


def replay_partial_target_import_shadow(
    *, source_object_ids: tuple[str, ...], imported_before_failure: tuple[str, ...]
) -> PartialTargetImportReplay:
    """Replay a partial import and resumable continuation without duplicate IDs."""

    source = _object_ids(source_object_ids, label="source snapshot")
    imported = _object_ids(imported_before_failure, label="partial target import")
    if not imported or len(imported) >= len(source):
        raise InstallationMigrationError("partial target import must be a strict non-empty subset")
    source_set = set(source)
    if not set(imported) <= source_set:
        raise InstallationMigrationError("partial target import contains an unknown source object")
    resumed = tuple(object_id for object_id in source if object_id not in set(imported))
    return PartialTargetImportReplay(
        source_object_ids=source,
        imported_before_failure=imported,
        resumed_after_failure=resumed,
        final_target_object_ids=source,
    )


def _safe_token(value: Any, *, label: str) -> str:
    text = str(value or "").strip()
    if not _SAFE_TOKEN.fullmatch(text) or any(marker.lower() in text.lower() for marker in _DISALLOWED_TEXT):
        raise InstallationMigrationError(f"unsafe migration {label}")
    return text


def _digest(value: Any, *, label: str) -> str:
    text = str(value or "").strip()
    if not _SHA256.fullmatch(text):
        raise InstallationMigrationError(f"migration {label} must be a sha256 digest")
    return text


def _portable_resource_uri(value: Any, *, label: str) -> str:
    text = str(value or "").strip()
    if not text.startswith(("knowledge://", "harness://")) or any(
        marker.lower() in text.lower() for marker in _DISALLOWED_TEXT
    ):
        raise InstallationMigrationError(f"{label} must use a portable Resource URI")
    if "\\" in text or ".." in text or any(ord(character) < 32 or ord(character) == 127 for character in text):
        raise InstallationMigrationError(f"{label} contains an unsafe path or control character")
    return text


@dataclass(frozen=True, slots=True)
class MigrationObjectSummary:
    domain: str
    object_count: int
    source_digest: str

    def __post_init__(self) -> None:
        if self.domain not in MIGRATION_DOMAINS:
            raise InstallationMigrationError("migration object domain is unsupported")
        if not isinstance(self.object_count, int) or self.object_count < 0:
            raise InstallationMigrationError("migration object count must be non-negative")
        _digest(self.source_digest, label="object source digest")

    def to_dict(self) -> dict[str, Any]:
        return {"domain": self.domain, "object_count": self.object_count, "source_digest": self.source_digest}


@dataclass(frozen=True, slots=True)
class CredentialRebind:
    slot: str
    source_ref_digest: str
    target_ref: str
    status: str

    def __post_init__(self) -> None:
        _safe_token(self.slot, label="credential slot")
        _digest(self.source_ref_digest, label="credential source ref")
        if (
            not self.target_ref.startswith("credential://")
            or "\\" in self.target_ref
            or ".." in self.target_ref
            or any(ord(character) < 32 or ord(character) == 127 for character in self.target_ref)
            or any(
            marker.lower() in self.target_ref.lower() for marker in _DISALLOWED_TEXT
            )
        ):
            raise InstallationMigrationError("credential target must be a non-secret credential URI")
        _safe_token(self.status, label="credential status")
        if self.status not in {"pending", "rebound", "failed"}:
            raise InstallationMigrationError("credential status is unsupported")

    def to_dict(self) -> dict[str, str]:
        return {
            "slot": self.slot,
            "source_ref_digest": self.source_ref_digest,
            "target_ref": self.target_ref,
            "status": self.status,
        }


@dataclass(frozen=True, slots=True)
class InstallationMigrationManifest:
    source_installation_id: str
    source_schema_revision: str
    source_catalog_revision: str
    target_versions: tuple[tuple[str, str], ...]
    object_summaries: tuple[MigrationObjectSummary, ...]
    id_resource_mappings: tuple[tuple[str, str], ...]
    credential_rebinds: tuple[CredentialRebind, ...]
    active_writers: tuple[tuple[str, str], ...]
    checkpoint: tuple[tuple[str, str], ...]
    rollback_strategy: str
    state: MigrationState = MigrationState.DISCOVERED
    snapshot_digest: str | None = None
    staging_namespace: str | None = None
    active_installation_revision: str | None = None
    rollback_evidence_digest: str | None = None
    recovery_evidence_digest: str | None = None
    post_cutover_delta_digest: str | None = None
    post_cutover_delta_count: int = 0
    rollback_reconciliation_digest: str | None = None
    rollback_delta_reconciled: bool = False
    failure_checkpoint: str | None = None
    recovery_count: int = 0
    started_at: str = "shadow-start"
    completed_at: str | None = None
    rollback_window_open: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "state", MigrationState(self.state))
        if self.source_installation_id.startswith("sha256:"):
            _digest(self.source_installation_id, label="source installation id")
        else:
            _safe_token(self.source_installation_id, label="source installation id")
        _safe_token(self.source_schema_revision, label="source schema revision")
        _safe_token(self.source_catalog_revision, label="source catalog revision")
        if not self.target_versions or len({key for key, _ in self.target_versions}) != len(self.target_versions):
            raise InstallationMigrationError("target versions must be unique and non-empty")
        for key, value in self.target_versions:
            _safe_token(key, label="target version key")
            _safe_token(value, label="target version")
        if len({item.domain for item in self.object_summaries}) != len(self.object_summaries) or {
            item.domain for item in self.object_summaries
        } != set(MIGRATION_DOMAINS):
            raise InstallationMigrationError("object summaries must cover every migration domain")
        if len({source_id for source_id, _ in self.id_resource_mappings}) != len(self.id_resource_mappings):
            raise InstallationMigrationError("source IDs in migration mapping must be unique")
        for source_id, resource_uri in self.id_resource_mappings:
            _safe_token(source_id, label="source object ID")
            _portable_resource_uri(resource_uri, label="resource mapping")
        if len({item.slot for item in self.credential_rebinds}) != len(self.credential_rebinds):
            raise InstallationMigrationError("credential slots must be unique")
        writers = dict(self.active_writers)
        if len(writers) != len(self.active_writers):
            raise InstallationMigrationError("active writer domains must be unique")
        if set(writers) != set(MIGRATION_DOMAINS):
            raise InstallationMigrationError("active writer map must cover every migration domain")
        if any(writer not in {SOURCE_WRITER, "puddingharness", "puddingknowledge"} for writer in writers.values()):
            raise InstallationMigrationError("active writer is unsupported")
        if len({key for key, _ in self.checkpoint}) != len(self.checkpoint):
            raise InstallationMigrationError("checkpoint keys must be unique")
        for key, value in self.checkpoint:
            if not _SAFE_CHECKPOINT_KEY.fullmatch(key):
                raise InstallationMigrationError("checkpoint key is unsafe")
            _safe_token(value, label="checkpoint value")
        _safe_token(self.rollback_strategy, label="rollback strategy")
        if self.rollback_strategy not in {"reverse_delta", "snapshot_restore", "no_write_until_finalized"}:
            raise InstallationMigrationError("rollback strategy is unsupported")
        for label, value in (
            ("snapshot digest", self.snapshot_digest),
            ("rollback evidence digest", self.rollback_evidence_digest),
            ("recovery evidence digest", self.recovery_evidence_digest),
            ("post-cutover delta digest", self.post_cutover_delta_digest),
            ("rollback reconciliation digest", self.rollback_reconciliation_digest),
        ):
            if value is not None:
                _digest(value, label=label)
        if not isinstance(self.post_cutover_delta_count, int) or self.post_cutover_delta_count < 0:
            raise InstallationMigrationError("post-cutover delta count must be non-negative")
        if not isinstance(self.rollback_delta_reconciled, bool):
            raise InstallationMigrationError("rollback delta reconciliation must be boolean")
        if self.failure_checkpoint is not None and not _SAFE_CHECKPOINT_KEY.fullmatch(self.failure_checkpoint):
            raise InstallationMigrationError("failure checkpoint is unsafe")
        if not isinstance(self.recovery_count, int) or self.recovery_count < 0:
            raise InstallationMigrationError("recovery count must be non-negative")
        for label, value in (
            ("staging namespace", self.staging_namespace),
            ("active installation revision", self.active_installation_revision),
        ):
            if value is not None:
                _safe_token(value, label=label)
        _safe_token(self.started_at, label="started at")
        if self.completed_at is not None:
            _safe_token(self.completed_at, label="completed at")
        self._validate_state(writers)

    def _validate_state(self, writers: dict[str, str]) -> None:
        state = MigrationState(self.state)
        if state is MigrationState.DISCOVERED:
            if (
                any(writer != SOURCE_WRITER for writer in writers.values())
                or self.snapshot_digest
                or self.staging_namespace
                or self.post_cutover_delta_digest
                or self.post_cutover_delta_count
                or self.rollback_reconciliation_digest
                or self.rollback_delta_reconciled
            ):
                raise InstallationMigrationError("discovered state must remain source-owned and unstaged")
            if self.rollback_window_open:
                raise InstallationMigrationError("discovered state cannot open rollback window")
        elif state is MigrationState.PREPARED:
            if (
                any(writer != SOURCE_WRITER for writer in writers.values())
                or not self.snapshot_digest
                or not self.staging_namespace
                or self.post_cutover_delta_digest
                or self.post_cutover_delta_count
                or self.rollback_reconciliation_digest
                or self.rollback_delta_reconciled
            ):
                raise InstallationMigrationError("prepared state requires source writer, snapshot, and staging")
            if "target_import" not in dict(self.checkpoint):
                raise InstallationMigrationError("prepared state requires a target import checkpoint")
            if not self.rollback_window_open:
                raise InstallationMigrationError("prepared state must open installation rollback window")
        elif state is MigrationState.CUTOVER:
            if writers != TARGET_WRITERS or not self.snapshot_digest or not self.staging_namespace or not self.active_installation_revision:
                raise InstallationMigrationError("cutover state requires target writers and active revision")
            if any(item.status != "rebound" for item in self.credential_rebinds):
                raise InstallationMigrationError("cutover requires every credential rebind to be confirmed")
            if self.failure_checkpoint is not None or dict(self.checkpoint).get("target_import") != "verified":
                raise InstallationMigrationError("cutover requires a target import checkpoint")
            if not self.rollback_window_open:
                raise InstallationMigrationError("cutover state must keep rollback window open")
            if self.post_cutover_delta_count == 0:
                if self.post_cutover_delta_digest or self.rollback_reconciliation_digest or self.rollback_delta_reconciled:
                    raise InstallationMigrationError("empty post-cutover delta cannot carry reconciliation evidence")
            elif (
                not self.post_cutover_delta_digest
                or dict(self.checkpoint).get("post_cutover_delta") != self.post_cutover_delta_digest
                or (self.rollback_delta_reconciled and not self.rollback_reconciliation_digest)
                or (self.rollback_reconciliation_digest and not self.rollback_delta_reconciled)
                or (
                    self.rollback_reconciliation_digest
                    and dict(self.checkpoint).get("rollback_reconciliation")
                    != self.rollback_reconciliation_digest
                )
            ):
                raise InstallationMigrationError("cutover delta must be captured before reconciliation")
        elif state is MigrationState.ROLLED_BACK:
            if any(writer != SOURCE_WRITER for writer in writers.values()) or not self.rollback_evidence_digest:
                raise InstallationMigrationError("rollback must restore source writers and record evidence")
            if not self.snapshot_digest or not self.staging_namespace or self.active_installation_revision:
                raise InstallationMigrationError("rollback must retain the snapshot but clear the target active revision")
            if "rollback" not in dict(self.checkpoint):
                raise InstallationMigrationError("rollback requires a rollback checkpoint")
            if not self.rollback_window_open:
                raise InstallationMigrationError("rollback remains inside installation rollback window")
            if self.post_cutover_delta_count > 0 and (
                not self.post_cutover_delta_digest
                or not self.rollback_delta_reconciled
                or not self.rollback_reconciliation_digest
                or dict(self.checkpoint).get("post_cutover_delta") != self.post_cutover_delta_digest
                or dict(self.checkpoint).get("rollback_reconciliation") != self.rollback_reconciliation_digest
            ):
                raise InstallationMigrationError("rollback must reconcile the post-cutover delta")
        elif state is MigrationState.FINALIZED:
            if writers != TARGET_WRITERS or not self.completed_at or self.rollback_window_open or not self.snapshot_digest or not self.staging_namespace or not self.active_installation_revision:
                raise InstallationMigrationError("finalized state must close the rollback window")
            if any(item.status != "rebound" for item in self.credential_rebinds):
                raise InstallationMigrationError("finalized state requires every credential rebind to be confirmed")
            if "finalized" not in dict(self.checkpoint):
                raise InstallationMigrationError("finalized state requires a completion checkpoint")

    def record_failure(self, checkpoint: str) -> InstallationMigrationManifest:
        """Record an injected PREPARED-stage failure without changing writers."""

        if MigrationState(self.state) is not MigrationState.PREPARED:
            raise InstallationMigrationError("only a prepared migration can record a resumable failure")
        if not _SAFE_CHECKPOINT_KEY.fullmatch(checkpoint) or checkpoint not in dict(self.checkpoint):
            raise InstallationMigrationError("failure checkpoint is not registered")
        updated = tuple((key, "failed" if key == checkpoint else value) for key, value in self.checkpoint)
        return replace(self, checkpoint=updated, failure_checkpoint=checkpoint)

    def recover(self, *, recovery_evidence_digest: str) -> InstallationMigrationManifest:
        """Resume a PREPARED migration after a recorded failure."""

        if MigrationState(self.state) is not MigrationState.PREPARED or self.failure_checkpoint is None:
            raise InstallationMigrationError("no resumable prepared failure exists")
        _digest(recovery_evidence_digest, label="recovery evidence")
        updated = tuple(
            (key, "verified" if key == self.failure_checkpoint else value) for key, value in self.checkpoint
        )
        return replace(
            self,
            checkpoint=updated + (("recovery", "resumed"),),
            recovery_evidence_digest=recovery_evidence_digest,
            failure_checkpoint=None,
            recovery_count=self.recovery_count + 1,
        )

    def record_credential_rebind_failure(self, slot: str) -> InstallationMigrationManifest:
        """Record a failed credential rebind without opening CUTOVER."""

        if MigrationState(self.state) is not MigrationState.PREPARED:
            raise InstallationMigrationError("credential rebind failure requires prepared state")
        _safe_token(slot, label="credential slot")
        rebinds = list(self.credential_rebinds)
        matching = [index for index, item in enumerate(rebinds) if item.slot == slot]
        if len(matching) != 1 or rebinds[matching[0]].status != "pending":
            raise InstallationMigrationError("credential slot is not pending for rebind")
        rebinds[matching[0]] = replace(rebinds[matching[0]], status="failed")
        checkpoint = dict(self.checkpoint)
        if checkpoint.get("credential_rebind") != "pending":
            raise InstallationMigrationError("credential rebind checkpoint is not pending")
        checkpoint["credential_rebind"] = "failed"
        return replace(
            self,
            credential_rebinds=tuple(rebinds),
            checkpoint=tuple(checkpoint.items()),
            failure_checkpoint="credential_rebind",
        )

    def recover_credential_rebind(
        self, *, slot: str, recovery_evidence_digest: str
    ) -> InstallationMigrationManifest:
        """Resume a failed rebind after opaque user reauthorization."""

        if MigrationState(self.state) is not MigrationState.PREPARED:
            raise InstallationMigrationError("credential rebind recovery requires prepared state")
        if self.failure_checkpoint != "credential_rebind" or dict(self.checkpoint).get("credential_rebind") != "failed":
            raise InstallationMigrationError("no failed credential rebind is resumable")
        _safe_token(slot, label="credential slot")
        _digest(recovery_evidence_digest, label="credential recovery evidence")
        rebinds = list(self.credential_rebinds)
        matching = [index for index, item in enumerate(rebinds) if item.slot == slot]
        if len(matching) != 1 or rebinds[matching[0]].status != "failed":
            raise InstallationMigrationError("credential slot is not failed for recovery")
        rebinds[matching[0]] = replace(rebinds[matching[0]], status="rebound")
        checkpoint = dict(self.checkpoint)
        checkpoint["credential_rebind"] = "verified"
        return replace(
            self,
            credential_rebinds=tuple(rebinds),
            checkpoint=tuple(checkpoint.items()) + (("credential_recovery", "resumed"),),
            recovery_evidence_digest=recovery_evidence_digest,
            failure_checkpoint=None,
            recovery_count=self.recovery_count + 1,
        )

    def record_post_cutover_delta(
        self, *, delta_digest: str, object_count: int
    ) -> InstallationMigrationManifest:
        """Record new target-owned objects observed after CUTOVER."""

        if MigrationState(self.state) is not MigrationState.CUTOVER:
            raise InstallationMigrationError("post-cutover delta requires cutover state")
        if self.post_cutover_delta_digest is not None or self.post_cutover_delta_count:
            raise InstallationMigrationError("post-cutover delta is already recorded")
        _digest(delta_digest, label="post-cutover delta")
        if not isinstance(object_count, int) or object_count < 1:
            raise InstallationMigrationError("post-cutover delta must contain at least one object")
        checkpoint = tuple(self.checkpoint) + (("post_cutover_delta", delta_digest),)
        return replace(
            self,
            checkpoint=checkpoint,
            post_cutover_delta_digest=delta_digest,
            post_cutover_delta_count=object_count,
        )

    def reconcile_post_cutover_delta(
        self, *, reconciliation_digest: str
    ) -> InstallationMigrationManifest:
        """Prove the post-cutover delta is reconciled before source rollback."""

        if MigrationState(self.state) is not MigrationState.CUTOVER:
            raise InstallationMigrationError("delta reconciliation requires cutover state")
        if not self.post_cutover_delta_digest or self.post_cutover_delta_count < 1:
            raise InstallationMigrationError("no post-cutover delta is available for reconciliation")
        if self.rollback_delta_reconciled:
            raise InstallationMigrationError("post-cutover delta is already reconciled")
        _digest(reconciliation_digest, label="rollback reconciliation")
        return replace(
            self,
            checkpoint=tuple(self.checkpoint) + (("rollback_reconciliation", reconciliation_digest),),
            rollback_reconciliation_digest=reconciliation_digest,
            rollback_delta_reconciled=True,
        )

    def transition(self, target: MigrationState, **changes: Any) -> InstallationMigrationManifest:
        target = MigrationState(target)
        current = MigrationState(self.state)
        if target is current:
            if changes:
                raise InstallationMigrationError("replaying a state cannot mutate the manifest")
            return self
        allowed = {
            MigrationState.DISCOVERED: {MigrationState.PREPARED},
            MigrationState.PREPARED: {MigrationState.CUTOVER},
            MigrationState.CUTOVER: {MigrationState.ROLLED_BACK, MigrationState.FINALIZED},
        }
        if target not in allowed.get(current, set()):
            raise InstallationMigrationError(f"invalid migration transition {current.value}->{target.value}")
        defaults: dict[str, Any] = {"state": target}
        if target is MigrationState.PREPARED:
            defaults.update({"rollback_window_open": True})
        elif target is MigrationState.CUTOVER:
            defaults.update({"active_writers": tuple(TARGET_WRITERS.items()), "rollback_window_open": True})
        elif target is MigrationState.ROLLED_BACK:
            defaults.update(
                {
                    "active_writers": tuple((domain, SOURCE_WRITER) for domain in MIGRATION_DOMAINS),
                    "active_installation_revision": None,
                    "rollback_window_open": True,
                }
            )
        elif target is MigrationState.FINALIZED:
            defaults.update({"active_writers": tuple(TARGET_WRITERS.items()), "rollback_window_open": False})
        defaults.update(changes)
        return replace(self, **defaults)

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": _FORMAT,
            "source": {
                "installation_id": self.source_installation_id,
                "schema_revision": self.source_schema_revision,
                "catalog_revision": self.source_catalog_revision,
            },
            "targets": dict(self.target_versions),
            "object_summaries": [item.to_dict() for item in self.object_summaries],
            "id_resource_mappings": [{"source_id": source_id, "resource_uri": uri} for source_id, uri in self.id_resource_mappings],
            "credential_rebinds": [item.to_dict() for item in self.credential_rebinds],
            "active_writers": dict(self.active_writers),
            "checkpoint": dict(self.checkpoint),
            "rollback_strategy": self.rollback_strategy,
            "state": self.state.value,
            "snapshot_digest": self.snapshot_digest,
            "staging_namespace": self.staging_namespace,
            "active_installation_revision": self.active_installation_revision,
            "rollback_evidence_digest": self.rollback_evidence_digest,
            "recovery_evidence_digest": self.recovery_evidence_digest,
            "post_cutover_delta_digest": self.post_cutover_delta_digest,
            "post_cutover_delta_count": self.post_cutover_delta_count,
            "rollback_reconciliation_digest": self.rollback_reconciliation_digest,
            "rollback_delta_reconciled": self.rollback_delta_reconciled,
            "failure_checkpoint": self.failure_checkpoint,
            "recovery_count": self.recovery_count,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "rollback_window_open": self.rollback_window_open,
        }


__all__ = [
    "CredentialRebind",
    "InstallationMigrationError",
    "InstallationMigrationManifest",
    "MIGRATION_DOMAINS",
    "MigrationObjectSummary",
    "MigrationState",
    "PartialTargetImportReplay",
    "StatefulRollbackReplay",
    "replay_partial_target_import_shadow",
    "replay_stateful_rollback_shadow",
    "SOURCE_WRITER",
    "TARGET_WRITERS",
]
