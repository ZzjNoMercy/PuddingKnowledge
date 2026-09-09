"""First-principles Catalog migration orchestration.

The slice adapters prove individual ownership conversions.  This module proves
the higher-level operational invariant from the specification: writes are
drained before one consistent source read, Platform and Harness targets are
staged independently, verification happens before any active-revision change,
and an injected failure leaves both targets at their pre-run state.

The orchestrator is deliberately provider-neutral.  A production drain
controller and a real Vault operator are injected by the caller; the module
never reaches into PuddingClaw runtime globals.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import Connection
from sqlalchemy.engine import Engine

from .authoring_job_rehearsal import _copy_authoring_slice
from .connector_rehearsal import _copy_connector_slice
from .credential_rehearsal import _copy_credential_slice
from .database_source_rehearsal import _copy_database_source_slice
from .harness_migrations import migrate_harness_to_latest
from .harness_models import HarnessWorkerAccessLog
from .migrations import migrate_to_latest
from .notification_event_rehearsal import _copy_notification_slice
from .processing_job_rehearsal import _copy_processing_slice
from .query_result_rehearsal import _copy_query_result_slice
from .read_later_rehearsal import _copy_read_later_slice
from .rehearsal import RehearsalReport, RehearsalVerificationError, build_table_snapshot
from .rehearsal_runner import (
    _engines_are_independent,
    _json_safe,
    _redact,
    _reflect_rows,
    _secrets_are_redacted,
    _target_database_state,
    _upsert_immutable,
    run_core_catalog_rehearsal,
)
from .table_asset_rehearsal import _copy_structured_asset_slice

_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
MIGRATION_FAILURE_CHECKPOINTS = frozenset({"after_drain", "after_schema", "after_copy", "before_verify"})
DEFAULT_MIGRATION_FAILURE_PROBES = ("after_drain", "after_schema", "after_copy", "before_verify")


class MigrationInjectedFailure(RuntimeError):
    """Controlled failure used to prove full-orchestration rollback."""


class MigrationDrainController(Protocol):
    """Runtime-owned write fence required before a migration can read the source."""

    def enter(self, *, installation_id: str, reason: str) -> None:
        """Fence relevant writers and enter the drain state."""

    def assert_drained(self) -> None:
        """Fail unless no relevant writer or in-flight job remains."""

    def exit(self) -> None:
        """Release the fence after staging succeeds or rolls back."""


@dataclass(frozen=True, slots=True)
class CatalogSlice:
    """One uniformly callable Platform ownership adapter."""

    name: str
    runner: Callable[..., tuple[RehearsalReport, tuple[tuple[str, str, str], ...]]]


@dataclass(frozen=True, slots=True)
class MigrationSliceEvidence:
    name: str
    mapping_count: int
    report: RehearsalReport

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "mapping_count": self.mapping_count, "report": self.report.to_dict()}


@dataclass(frozen=True, slots=True)
class CatalogMigrationManifest:
    installation_id: str
    source_revision: str
    target_revision: str
    active_revision_before: str
    active_revision_after: str
    state: str
    source_before_digest: str
    source_after_digest: str
    platform_target_digest: str
    harness_target_digest: str
    slices: tuple[MigrationSliceEvidence, ...]
    failure_checkpoints: tuple[str, ...]
    active_writers: dict[str, str]

    @property
    def active_revision_changed(self) -> bool:
        return self.active_revision_before != self.active_revision_after

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": "agent-knowledge-platform-catalog-migration/v1",
            "installation_id": self.installation_id,
            "source_revision": self.source_revision,
            "target_revision": self.target_revision,
            "active_revision_before": self.active_revision_before,
            "active_revision_after": self.active_revision_after,
            "active_revision_changed": self.active_revision_changed,
            "state": self.state,
            "source": {"before_digest": self.source_before_digest, "after_digest": self.source_after_digest},
            "targets": {
                "platform": {"digest": self.platform_target_digest},
                "harness": {"digest": self.harness_target_digest},
            },
            "active_writers": dict(self.active_writers),
            "failure_checkpoints": list(self.failure_checkpoints),
            "slices": [item.to_dict() for item in self.slices],
            "rollback_strategy": "fence-writers-then-transactional-staging; active revision remains unchanged",
        }

    def write_json(self, path: str | Path) -> None:
        Path(path).write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )


def _validate_identifier(value: Any, *, label: str) -> str:
    text = str(value or "").strip()
    if not _SAFE_IDENTIFIER.fullmatch(text):
        raise RehearsalVerificationError(f"unsafe catalog migration {label}")
    return text


def _state_digest(state: tuple[tuple[str, int, str, str], ...]) -> str:
    payload = json.dumps(state, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _copy_harness_worker_access_logs(
    source: Connection,
    target: Connection,
    *,
    source_revision: str,
    target_revision: str,
    active_revision: str,
    failure_checkpoint: str | None = None,
) -> tuple[RehearsalReport, tuple[tuple[str, str, str], ...]]:
    """Copy the Harness-owned audit table without importing the legacy ORM."""

    rows = _reflect_rows(source, "worker_access_logs", ("id", "key_id", "key_name", "query", "created_at"))
    canonical = []
    for row in rows:
        sanitized = dict(row)
        # Harness audit text is still an untrusted boundary payload. Keep a
        # useful query when it is ordinary text, but remove a whole payload
        # containing secret-like material before it reaches the target DB.
        sanitized["key_name"] = _redact(sanitized.get("key_name"), key="key_name")
        sanitized["query"] = _redact(sanitized.get("query"), key="query")
        canonical.append(sanitized)
    table = HarnessWorkerAccessLog.__table__
    for row in canonical:
        _upsert_immutable(target, table, row, ("id",))
    for row in canonical:
        _upsert_immutable(target, table, row, ("id",))
    target_rows = [
        dict(row)
        for row in target.execute(
            table.select().with_only_columns(
                table.c.id, table.c.key_id, table.c.key_name, table.c.query, table.c.created_at
            )
        ).mappings().all()
    ]
    snapshot_source = build_table_snapshot(
        "worker_access_logs", [{key: _json_safe(value) for key, value in row.items()} for row in canonical], primary_key_fields=("id",)
    )
    snapshot_target = build_table_snapshot(
        "worker_access_logs",
        [{key: _json_safe(value) for key, value in row.items()} for row in target_rows],
        primary_key_fields=("id",),
    )
    report = RehearsalReport(
        source_revision=source_revision,
        target_revision=target_revision,
        active_revision_before=active_revision,
        active_revision_after=active_revision,
        source_tables=(snapshot_source,),
        target_tables=(snapshot_target,),
        retry_idempotent=True,
        checks={
            "secret_redaction": all(
                _secrets_are_redacted(row.get(field), key=field)
                for row in canonical
                for field in ("key_name", "query")
            ),
            "file_reachability": True,
            "lease_state": True,
            "foreign_keys": True,
        },
        check_scopes={
            "secret_redaction": "Harness audit payload remains in Harness and is not copied to Platform",
            "file_reachability": "WorkerAccessLog has no file reference",
            "lease_state": "WorkerAccessLog has no migration lease",
            "foreign_keys": "Harness audit rows are independent of Platform Catalog FKs",
        },
    )
    report.verify_safe()
    mappings = tuple(
        ("worker_access_log", str(row["id"]), f"harness://worker-access-logs/{row['id']}") for row in canonical
    )
    return report, mappings


def _default_slices(
    *,
    installation_id: str,
    file_reference_checker: Callable[[str], bool] | None = None,
    lease_as_of: datetime | None = None,
) -> tuple[CatalogSlice, ...]:
    """Return the ownership-ordered adapters used by the full rehearsal."""

    common = {"file_reference_checker": file_reference_checker}

    def run_core(
        source: Connection, target: Connection, **kwargs: Any
    ) -> tuple[RehearsalReport, tuple[tuple[str, str, str], ...]]:
        # The core adapter returns a rich result object so its source/target
        # table inventory is available to callers.  The orchestration layer
        # consumes the common (report, mappings) protocol used by every
        # ownership slice; normalize at this boundary instead of making the
        # orchestrator know about one adapter's richer return type.
        result = run_core_catalog_rehearsal(source, target, installation_id=installation_id, **kwargs, **common)
        return result.report, result.source_to_target

    return (
        CatalogSlice("core", run_core),
        CatalogSlice(
            "connector",
            lambda source, target, **kwargs: _copy_connector_slice(
                source, target, **kwargs, **common, lease_as_of=lease_as_of
            ),
        ),
        CatalogSlice("credential", _copy_credential_slice),
        CatalogSlice(
            "read_later",
            lambda source, target, **kwargs: _copy_read_later_slice(
                source, target, **kwargs, **common, lease_as_of=lease_as_of
            ),
        ),
        CatalogSlice(
            "structured_asset",
            lambda source, target, **kwargs: _copy_structured_asset_slice(source, target, **kwargs, **common),
        ),
        CatalogSlice(
            "query_result",
            lambda source, target, **kwargs: _copy_query_result_slice(source, target, **kwargs, **common),
        ),
        CatalogSlice(
            "processing_job",
            lambda source, target, **kwargs: _copy_processing_slice(
                source, target, **kwargs, **common, lease_as_of=lease_as_of
            ),
        ),
        CatalogSlice(
            "authoring_job",
            lambda source, target, **kwargs: _copy_authoring_slice(
                source, target, **kwargs, **common, lease_as_of=lease_as_of
            ),
        ),
        CatalogSlice("database_source", _copy_database_source_slice),
        CatalogSlice("notification_event", _copy_notification_slice),
    )


def default_catalog_slices(
    *,
    installation_id: str = "catalog-migration",
    file_reference_checker: Callable[[str], bool] | None = None,
    lease_as_of: datetime | None = None,
) -> tuple[CatalogSlice, ...]:
    """Expose the ownership-ordered full Platform slice set for CLI/adapters."""

    return _default_slices(
        installation_id=installation_id, file_reference_checker=file_reference_checker, lease_as_of=lease_as_of
    )


def run_catalog_migration_rehearsal(
    source_engine: Engine,
    platform_engine: Engine,
    harness_engine: Engine,
    *,
    installation_id: str,
    source_revision: str,
    target_revision: str,
    active_revision: str,
    drain_controller: MigrationDrainController,
    slices: Sequence[CatalogSlice] | None = None,
    harness_runner: Callable[..., tuple[RehearsalReport, tuple[tuple[str, str, str], ...]]] =
    _copy_harness_worker_access_logs,
    failure_checkpoint: str | None = None,
    failure_checkpoints: Sequence[str] = (),
    file_reference_checker: Callable[[str], bool] | None = None,
    lease_as_of: datetime | None = None,
) -> CatalogMigrationManifest:
    """Stage and verify two independent Catalogs while keeping source authoritative.

    ``failure_checkpoint`` is for one probe invocation.  ``failure_checkpoints``
    is recorded in the manifest and is normally supplied by a surrounding test
    loop; the function itself never changes the active revision.
    """

    for value, label in (
        (installation_id, "installation id"),
        (source_revision, "source revision"),
        (target_revision, "target revision"),
        (active_revision, "active revision"),
    ):
        _validate_identifier(value, label=label)
    if source_revision == target_revision:
        raise RehearsalVerificationError("source and target revisions must differ")
    if failure_checkpoint is not None and failure_checkpoint not in MIGRATION_FAILURE_CHECKPOINTS:
        raise ValueError(f"unsupported catalog migration failure checkpoint: {failure_checkpoint}")
    unknown = sorted(set(failure_checkpoints) - MIGRATION_FAILURE_CHECKPOINTS)
    if unknown:
        raise ValueError(f"unsupported catalog migration failure checkpoints: {unknown}")
    if not all(
        _engines_are_independent(left, right)
        for left, right in ((source_engine, platform_engine), (source_engine, harness_engine), (platform_engine, harness_engine))
    ):
        raise RehearsalVerificationError("source, Platform and Harness must use independent engines")

    # Schema preparation is a reversible-independent preflight. The rollback
    # window starts after both target histories exist, so failures compare the
    # complete staged target state rather than conflating DDL bootstrapping
    # with row-copy rollback.
    with platform_engine.begin() as platform_target:
        migrate_to_latest(platform_target)
    with harness_engine.begin() as harness_target:
        migrate_harness_to_latest(harness_target)
    platform_before = _target_database_state(platform_engine)
    harness_before = _target_database_state(harness_engine)
    selected_slices = tuple(slices) if slices is not None else _default_slices(
        installation_id=installation_id, file_reference_checker=file_reference_checker, lease_as_of=lease_as_of
    )
    if not selected_slices:
        raise ValueError("at least one Catalog slice is required")
    names = [item.name for item in selected_slices]
    if len(set(names)) != len(names):
        raise ValueError("Catalog slice names must be unique")

    entered = False
    source_before: tuple[tuple[str, int, str, str], ...] | None = None
    try:
        drain_controller.enter(installation_id=installation_id, reason="catalog migration rehearsal")
        entered = True
        drain_controller.assert_drained()
        source_before = _target_database_state(source_engine)
        if failure_checkpoint == "after_drain":
            raise MigrationInjectedFailure("injected catalog migration failure at after_drain")

        evidence: list[MigrationSliceEvidence] = []
        with platform_engine.begin() as platform_target, harness_engine.begin() as harness_target:
            if failure_checkpoint == "after_schema":
                raise MigrationInjectedFailure("injected catalog migration failure at after_schema")
            with source_engine.connect() as source:
                for item in selected_slices:
                    report, mappings = item.runner(
                        source,
                        platform_target,
                        source_revision=source_revision,
                        target_revision=target_revision,
                        active_revision=active_revision,
                        failure_checkpoint=None,
                    )
                    if report.active_revision_changed:
                        raise RehearsalVerificationError(f"slice {item.name} changed active revision")
                    if report.target_out_of_scope_tables:
                        raise RehearsalVerificationError(
                            f"slice {item.name} left out-of-scope rows in the target Catalog"
                        )
                    evidence.append(MigrationSliceEvidence(item.name, len(mappings), report))
                report, mappings = harness_runner(
                    source,
                    harness_target,
                    source_revision=source_revision,
                    target_revision=target_revision,
                    active_revision=active_revision,
                    failure_checkpoint=None,
                )
                if report.active_revision_changed:
                    raise RehearsalVerificationError("Harness slice changed active revision")
                if report.target_out_of_scope_tables:
                    raise RehearsalVerificationError("Harness target contains out-of-scope rows")
                evidence.append(MigrationSliceEvidence("harness_worker_access_log", len(mappings), report))
            if failure_checkpoint == "after_copy":
                raise MigrationInjectedFailure("injected catalog migration failure at after_copy")
            if failure_checkpoint == "before_verify":
                raise MigrationInjectedFailure("injected catalog migration failure at before_verify")
            if not evidence or not all(all(item.report.checks.values()) for item in evidence):
                raise RehearsalVerificationError("Catalog slice verification did not pass")

        source_after = _target_database_state(source_engine)
        if source_before is None or source_after != source_before:
            raise RehearsalVerificationError("source Catalog changed during migration")
        platform_after = _target_database_state(platform_engine)
        harness_after = _target_database_state(harness_engine)
        if platform_after == platform_before or harness_after == harness_before:
            raise RehearsalVerificationError("migration did not produce both independent target Catalogs")
        return CatalogMigrationManifest(
            installation_id=installation_id,
            source_revision=source_revision,
            target_revision=target_revision,
            active_revision_before=active_revision,
            active_revision_after=active_revision,
            state="VERIFIED",
            source_before_digest=_state_digest(source_before),
            source_after_digest=_state_digest(source_after),
            platform_target_digest=_state_digest(platform_after),
            harness_target_digest=_state_digest(harness_after),
            slices=tuple(evidence),
            failure_checkpoints=tuple(failure_checkpoints),
            active_writers={"legacy_catalog": "puddingclaw", "platform_catalog": "staging", "harness": "puddingclaw"},
        )
    except Exception as error:
        if source_before is not None and _target_database_state(source_engine) != source_before:
            raise RehearsalVerificationError(
                f"source Catalog changed during migration rollback (original error: {error})"
            ) from error
        if _target_database_state(platform_engine) != platform_before:
            raise RehearsalVerificationError(
                f"Platform target changed after migration rollback (original error: {error})"
            ) from error
        if _target_database_state(harness_engine) != harness_before:
            raise RehearsalVerificationError(
                f"Harness target changed after migration rollback (original error: {error})"
            ) from error
        raise
    finally:
        if entered:
            drain_controller.exit()
