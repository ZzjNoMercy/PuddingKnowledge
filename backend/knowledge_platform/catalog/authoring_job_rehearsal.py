"""Executable Phase 0B rehearsal for Semantic Authoring jobs."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, inspect
from sqlalchemy.engine import Engine

from .connector_rehearsal import _as_utc, _sanitize_path_value
from .migrations import migrate_to_latest
from .models import KnowledgeAuthoringEvent, KnowledgeAuthoringJob
from .rehearsal import RehearsalReport, RehearsalVerificationError, build_table_snapshot
from .rehearsal_runner import (
    RehearsalInjectedFailure,
    _json_safe,
    _reflect_rows,
    _secrets_are_redacted,
    _source_ref_digest,
    _table_snapshot_rows,
    _target_database_state,
    _upsert_immutable,
)

AUTHORING_JOB_SOURCE_TABLES = ("semantic_dimension_build_jobs", "semantic_dimension_build_events")
AUTHORING_JOB_TARGET_TABLES = ("knowledge_authoring_jobs", "knowledge_authoring_events")
AUTHORING_JOB_FAILURE_CHECKPOINTS = frozenset(
    {"after_schema", "after_authoring_jobs", "after_authoring_events", "before_verification"}
)
DEFAULT_AUTHORING_JOB_FAILURE_PROBES = (
    "after_schema",
    "after_authoring_jobs",
    "after_authoring_events",
    "before_verification",
)
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,239}$")
_STATUSES = {
    "queued",
    "running",
    "waiting_for_publish_confirmation",
    "waiting_for_baseline_change_confirmation",
    "published",
    "failed",
    "cancelled",
}
_NO_LEASE_STATUSES = _STATUSES - {"running"}
_EVENT_LEVELS = {"info", "warning", "error"}


def _validate_identifier(value: Any, *, label: str, max_length: int = 240) -> str:
    text = str(value or "")
    if len(text) > max_length or not _SAFE_IDENTIFIER.fullmatch(text):
        raise RehearsalVerificationError(f"unsafe authoring-job {label}")
    return text


def _job_id(value: Any) -> str:
    return f"authoring_{value}"


def _event_id(value: Any) -> str:
    return f"authoring_event_{value}"


def _authoring_physical_references(value: Any, *, key: str | None = None) -> list[str]:
    """Find local/file references, including relative paths in nested metadata."""

    if isinstance(value, str):
        is_path = value.startswith(("/", "\\\\", "//", "./", "../", "file://"))
        is_named_path = key is not None and re.search(
            r"(?:path|file|artifact|storage|snapshot|reference)$", key, re.IGNORECASE
        )
        return [value] if is_path or is_named_path else []
    if isinstance(value, Mapping):
        return [
            reference
            for child_key, child in value.items()
            for reference in _authoring_physical_references(child, key=str(child_key))
        ]
    if isinstance(value, (list, tuple)):
        return [reference for child in value for reference in _authoring_physical_references(child, key=key)]
    return []


def _digest_or_empty(value: Any) -> str:
    text = str(value or "")
    return _source_ref_digest(text) if text else ""


def _safe_text(value: Any) -> str:
    sanitized = _sanitize_path_value(str(value or ""))
    return sanitized if isinstance(sanitized, str) else json.dumps(sanitized, ensure_ascii=False, sort_keys=True)


def _safe_json(value: Any) -> Any:
    return _json_safe(_sanitize_path_value(value if value is not None else {}))


def _source_rows(source: Connection) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    jobs = _reflect_rows(
        source,
        "semantic_dimension_build_jobs",
        (
            "id",
            "session_id",
            "query_id",
            "dimension_id",
            "adapter",
            "requested_scope",
            "input_snapshot",
            "status",
            "current_step",
            "progress",
            "staging_path",
            "published_reference_path",
            "result_summary",
            "error_message",
            "retry_count",
            "created_at",
            "updated_at",
            "started_at",
            "finished_at",
            "lease_owner",
            "lease_expires_at",
            "heartbeat_at",
            "attempt",
        ),
    )
    events = _reflect_rows(
        source,
        "semantic_dimension_build_events",
        ("id", "job_id", "level", "message", "event_metadata", "created_at"),
    )
    return jobs, events


def _validate_rows(jobs: Sequence[Mapping[str, Any]], events: Sequence[Mapping[str, Any]]) -> None:
    job_ids: set[str] = set()
    for row in jobs:
        source_id = _validate_identifier(row["id"], label="id", max_length=150)
        _validate_identifier(row["dimension_id"], label="dimension id")
        _validate_identifier(row["adapter"], label="adapter")
        if source_id in job_ids:
            raise RehearsalVerificationError(f"duplicate authoring-job id: {source_id}")
        job_ids.add(source_id)
        status = str(row.get("status") or "queued")
        if status not in _STATUSES:
            raise RehearsalVerificationError(f"authoring-job {source_id}: unknown status")
        if not 0 <= int(row.get("progress") or 0) <= 100:
            raise RehearsalVerificationError(f"authoring-job {source_id}: invalid progress")
        if int(row.get("retry_count") or 0) < 0 or int(row.get("attempt") or 0) < 0:
            raise RehearsalVerificationError(f"authoring-job {source_id}: invalid retry/attempt")
        if row.get("lease_owner"):
            _validate_identifier(row["lease_owner"], label="lease owner", max_length=160)
        current_step = str(row.get("current_step") or "")
        if status == "queued" and (current_step != "queued" or int(row.get("progress") or 0) != 0):
            raise RehearsalVerificationError(f"authoring-job {source_id}: queued state invariant failed")
        if status == "running" and (
            not current_step or row.get("started_at") is None or row.get("finished_at") is not None
        ):
            raise RehearsalVerificationError(f"authoring-job {source_id}: running state invariant failed")
        if status in {"waiting_for_publish_confirmation", "waiting_for_baseline_change_confirmation"} and (
            current_step != status
            or int(row.get("progress") or 0) != 100
            or row.get("started_at") is None
            or row.get("finished_at") is None
        ):
            raise RehearsalVerificationError(f"authoring-job {source_id}: waiting state invariant failed")
        if status == "published" and (
            current_step != "published" or int(row.get("progress") or 0) != 100 or row.get("finished_at") is None
        ):
            raise RehearsalVerificationError(f"authoring-job {source_id}: published state invariant failed")
        if status == "failed" and (
            current_step != "failed" or row.get("finished_at") is None or not str(row.get("error_message") or "")
        ):
            raise RehearsalVerificationError(f"authoring-job {source_id}: failed state invariant failed")
        if status == "cancelled" and (current_step != "cancelled" or row.get("finished_at") is None):
            raise RehearsalVerificationError(f"authoring-job {source_id}: cancelled state invariant failed")
    event_ids: set[str] = set()
    for row in events:
        event_id = _validate_identifier(row["id"], label="event id", max_length=144)
        if event_id in event_ids:
            raise RehearsalVerificationError(f"duplicate authoring-event id: {event_id}")
        event_ids.add(event_id)
        if str(row.get("level") or "info") not in _EVENT_LEVELS:
            raise RehearsalVerificationError(f"event {event_id}: unknown event level")
        if _validate_identifier(row["job_id"], label="event job id", max_length=150) not in job_ids:
            raise RehearsalVerificationError(f"event {row['id']}: missing authoring job")


def _validate_leases(jobs: Sequence[Mapping[str, Any]], *, as_of: datetime | None) -> bool:
    as_of_utc = _as_utc(as_of)
    for row in jobs:
        status = str(row.get("status") or "queued")
        owner = str(row.get("lease_owner") or "")
        expiry = _as_utc(row.get("lease_expires_at"))
        heartbeat = _as_utc(row.get("heartbeat_at"))
        if status == "running":
            if not owner or expiry is None or heartbeat is None or as_of_utc is None:
                return False
            if heartbeat > expiry or expiry < as_of_utc or heartbeat > as_of_utc:
                return False
            updated_at = _as_utc(row.get("updated_at"))
            if updated_at is None or updated_at > as_of_utc:
                return False
        elif status in _NO_LEASE_STATUSES and (owner or expiry is not None or heartbeat is not None):
            return False
    return not any(str(row.get("status") or "queued") == "running" for row in jobs) or as_of_utc is not None


def _canonical_job(row: Mapping[str, Any]) -> dict[str, Any]:
    source_id = _validate_identifier(row["id"], label="id", max_length=150)
    dimension_id = _validate_identifier(row["dimension_id"], label="dimension id")
    target_id = _job_id(source_id)
    has_staging = bool(row.get("staging_path"))
    has_published = bool(row.get("published_reference_path"))
    return {
        "id": target_id,
        "kind": "semantic_dimension_build",
        "dimension_id": dimension_id,
        "adapter": _validate_identifier(row["adapter"], label="adapter"),
        "scope_uri": f"knowledge://semantic-dimensions/{dimension_id}",
        "scope_json": _safe_json(row.get("requested_scope") or {}),
        "input_snapshot_json": _safe_json(row.get("input_snapshot") or {}),
        "status": str(row.get("status") or "queued"),
        "current_step": _safe_text(row.get("current_step") or "queued"),
        "progress": int(row.get("progress") or 0),
        "staging_uri": f"knowledge://authoring-jobs/{target_id}/staging" if has_staging else "",
        "staging_reference_digest": _digest_or_empty(row.get("staging_path")),
        "published_uri": f"knowledge://authoring-jobs/{target_id}/published" if has_published else "",
        "published_reference_digest": _digest_or_empty(row.get("published_reference_path")),
        "result_summary_json": _safe_json(row.get("result_summary") or {}),
        "correlation_json": {
            "session_id_digest": _digest_or_empty(row.get("session_id")),
            "query_id_digest": _digest_or_empty(row.get("query_id")),
        },
        "error_message": _safe_text(row.get("error_message")),
        "retry_count": int(row.get("retry_count") or 0),
        "metadata_json": {},
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "started_at": row.get("started_at"),
        "finished_at": row.get("finished_at"),
        "lease_owner": _safe_text(row.get("lease_owner")) if row.get("lease_owner") else None,
        "lease_expires_at": row.get("lease_expires_at"),
        "heartbeat_at": row.get("heartbeat_at"),
        "attempt": int(row.get("attempt") or 0),
    }


def _canonical_event(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": _event_id(_validate_identifier(row["id"], label="event id", max_length=144)),
        "job_id": _job_id(_validate_identifier(row["job_id"], label="event job id", max_length=150)),
        "level": str(row.get("level") or "info"),
        "message": _safe_text(row.get("message")),
        "metadata_json": _safe_json(row.get("event_metadata") or {}),
        "created_at": row.get("created_at"),
    }


def _safe_out_of_scope_snapshot(target: Connection, table: Any, expected: Sequence[tuple[Any, ...]]) -> Any:
    rows = _table_snapshot_rows(
        target, table, fields=tuple(table.c.keys()), primary_key_fields=("id",), expected_primary_keys=None
    )
    extras = [row for row in rows if (row.get("id"),) not in set(expected)]
    if not extras:
        return None
    return build_table_snapshot(
        f"{table.name}:out_of_scope",
        [_json_safe(_sanitize_path_value(row)) for row in extras],
        primary_key_fields=("id",),
    )


def _copy_authoring_slice(
    source: Connection,
    target: Connection,
    *,
    source_revision: str,
    target_revision: str,
    active_revision: str,
    failure_checkpoint: str | None = None,
    lease_as_of: datetime | None = None,
    file_reference_checker: Callable[[str], bool] | None = None,
) -> tuple[RehearsalReport, tuple[tuple[str, str, str], ...]]:
    if failure_checkpoint is not None and failure_checkpoint not in AUTHORING_JOB_FAILURE_CHECKPOINTS:
        raise ValueError(f"unsupported authoring-job failure checkpoint: {failure_checkpoint}")
    jobs, events = _source_rows(source)
    _validate_identifier(source_revision, label="source revision")
    _validate_rows(jobs, events)
    physical_references = sorted(
        {
            reference
            for row in jobs
            for reference in (
                [str(row[field]) for field in ("staging_path", "published_reference_path") if row.get(field)]
                + [
                    reference
                    for field in ("requested_scope", "input_snapshot", "result_summary")
                    for reference in _authoring_physical_references(row.get(field), key=field)
                ]
            )
        }
    )
    if physical_references and file_reference_checker is None:
        raise RehearsalVerificationError("file_reference_checker is required for authoring-job paths")
    file_reachable = not physical_references or all(file_reference_checker(ref) for ref in physical_references)
    if not file_reachable:
        raise RehearsalVerificationError("authoring-job file-reference reachability check failed")
    canonical_jobs = [_canonical_job(row) for row in jobs]
    canonical_events = [_canonical_event(row) for row in events]
    migrate_to_latest(target)
    schema_ready = inspect(target).has_table(KnowledgeAuthoringJob.__table__.name) and inspect(target).has_table(
        KnowledgeAuthoringEvent.__table__.name
    )
    if failure_checkpoint == "after_schema":
        raise RehearsalInjectedFailure("injected authoring-job rehearsal failure at after_schema")
    job_table = KnowledgeAuthoringJob.__table__
    event_table = KnowledgeAuthoringEvent.__table__
    for row in canonical_jobs:
        _upsert_immutable(target, job_table, row, ("id",))
    if failure_checkpoint == "after_authoring_jobs":
        raise RehearsalInjectedFailure("injected authoring-job rehearsal failure at after_authoring_jobs")
    for row in canonical_events:
        _upsert_immutable(target, event_table, row, ("id",))
    if failure_checkpoint == "after_authoring_events":
        raise RehearsalInjectedFailure("injected authoring-job rehearsal failure at after_authoring_events")
    first_job_rows = _table_snapshot_rows(
        target,
        job_table,
        fields=tuple(job_table.c.keys()),
        primary_key_fields=("id",),
        expected_primary_keys=tuple((row["id"],) for row in canonical_jobs),
    )
    first_event_rows = _table_snapshot_rows(
        target,
        event_table,
        fields=tuple(event_table.c.keys()),
        primary_key_fields=("id",),
        expected_primary_keys=tuple((row["id"],) for row in canonical_events),
    )
    for row in canonical_jobs:
        _upsert_immutable(target, job_table, row, ("id",))
    for row in canonical_events:
        _upsert_immutable(target, event_table, row, ("id",))
    if failure_checkpoint == "before_verification":
        raise RehearsalInjectedFailure("injected authoring-job rehearsal failure at before_verification")
    target_job_rows = _table_snapshot_rows(
        target,
        job_table,
        fields=tuple(job_table.c.keys()),
        primary_key_fields=("id",),
        expected_primary_keys=tuple((row["id"],) for row in canonical_jobs),
    )
    target_event_rows = _table_snapshot_rows(
        target,
        event_table,
        fields=tuple(event_table.c.keys()),
        primary_key_fields=("id",),
        expected_primary_keys=tuple((row["id"],) for row in canonical_events),
    )
    retry_idempotent = first_job_rows == target_job_rows and first_event_rows == target_event_rows
    target_job_ids = {row["id"] for row in target_job_rows}
    foreign_keys_valid = all(row["job_id"] in target_job_ids for row in target_event_rows)
    source_snapshots = (
        build_table_snapshot(
            "authoring_jobs",
            [_json_safe(row) for row in canonical_jobs],
            primary_key_fields=("id",),
            lease_fields=("status", "lease_owner", "lease_expires_at", "heartbeat_at", "attempt"),
        ),
        build_table_snapshot(
            "authoring_events", [_json_safe(row) for row in canonical_events], primary_key_fields=("id",)
        ),
    )
    target_snapshots = (
        build_table_snapshot(
            "authoring_jobs",
            target_job_rows,
            primary_key_fields=("id",),
            lease_fields=("status", "lease_owner", "lease_expires_at", "heartbeat_at", "attempt"),
        ),
        build_table_snapshot("authoring_events", target_event_rows, primary_key_fields=("id",)),
    )
    out_of_scope = tuple(
        snapshot
        for table, expected in (
            (job_table, tuple((row["id"],) for row in canonical_jobs)),
            (event_table, tuple((row["id"],) for row in canonical_events)),
        )
        if (snapshot := _safe_out_of_scope_snapshot(target, table, expected)) is not None
    )
    secret_values = [
        row[field]
        for row in canonical_jobs
        for field in ("scope_json", "input_snapshot_json", "result_summary_json", "correlation_json", "error_message")
    ] + [row[field] for row in canonical_events for field in ("message", "metadata_json")]
    report = RehearsalReport(
        source_revision=source_revision,
        target_revision=target_revision,
        active_revision_before=active_revision,
        active_revision_after=active_revision,
        source_tables=source_snapshots,
        target_tables=target_snapshots,
        target_out_of_scope_tables=out_of_scope,
        retry_idempotent=retry_idempotent,
        checks={
            "secret_redaction": all(_secrets_are_redacted(value) for value in secret_values),
            "file_reachability": file_reachable,
            "lease_state": _validate_leases(canonical_jobs, as_of=lease_as_of),
            "foreign_keys": foreign_keys_valid,
            "schema_preflight": schema_ready,
        },
        check_scopes={
            "secret_redaction": "semantic scope/input/result metadata, correlation IDs, errors and events",
            "file_reachability": "physical staging/published paths and nested metadata paths checked by explicit checker",
            "lease_state": "running authoring jobs require owner, heartbeat, future expiry and explicit as_of; waiting states hold no lease",
            "foreign_keys": "event-to-job relationship is checked before copy; Session/Query are not ownership FKs",
            "schema_preflight": "v7 schema/history are prepared before the data transaction; SQLite DDL rollback is not claimed by after_schema",
        },
    )
    report.verify_safe()
    mappings = tuple(
        [
            (
                "semantic_dimension_build_job",
                str(row["id"]),
                f"knowledge://semantic-dimensions/{row['dimension_id']}/authoring-jobs/{_job_id(row['id'])}",
            )
            for row in jobs
        ]
        + [
            ("semantic_dimension_build_event", str(row["id"]), f"knowledge://authoring-events/{_event_id(row['id'])}")
            for row in events
        ]
    )
    return report, mappings


@dataclass(frozen=True, slots=True)
class AuthoringJobCatalogRehearsalResult:
    installation_id: str
    source_tables: tuple[str, ...]
    target_tables: tuple[str, ...]
    excluded_source_tables: tuple[str, ...]
    source_to_target: tuple[tuple[str, str, str], ...]
    report: RehearsalReport

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": "agent-knowledge-platform-catalog-rehearsal/v1",
            "installation_id": self.installation_id,
            "source_tables": list(self.source_tables),
            "target_tables": list(self.target_tables),
            "excluded_source_tables": list(self.excluded_source_tables),
            "source_to_target": [
                {"source_type": kind, "source_id": source_id, "target_uri": uri}
                for kind, source_id, uri in self.source_to_target
            ],
            "report": self.report.to_dict(),
        }

    def write_json(self, path: str | Path) -> None:
        Path(path).write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )


def run_authoring_job_catalog_rehearsal_with_rollback_probes(
    source: Connection,
    target_engine: Engine,
    *,
    installation_id: str,
    source_revision: str,
    target_revision: str,
    active_revision: str,
    failure_checkpoints: Sequence[str] = DEFAULT_AUTHORING_JOB_FAILURE_PROBES,
    file_reference_checker: Callable[[str], bool] | None = None,
    lease_as_of: datetime | None = None,
) -> AuthoringJobCatalogRehearsalResult:
    for value, label in (
        (installation_id, "installation id"),
        (source_revision, "source revision"),
        (target_revision, "target revision"),
        (active_revision, "active revision"),
    ):
        _validate_identifier(value, label=label)
    checkpoints = tuple(failure_checkpoints)
    unknown = sorted(set(checkpoints) - AUTHORING_JOB_FAILURE_CHECKPOINTS)
    if not checkpoints:
        raise ValueError("failure_checkpoints must not be empty")
    if unknown:
        raise ValueError(f"unsupported failure_checkpoints: {unknown}")
    with target_engine.begin() as target:
        migrate_to_latest(target)
    baseline = _target_database_state(target_engine)
    for checkpoint in checkpoints:
        try:
            with target_engine.begin() as target:
                _copy_authoring_slice(
                    source,
                    target,
                    source_revision=source_revision,
                    target_revision=f"{target_revision}-probe-{checkpoint}",
                    active_revision=active_revision,
                    failure_checkpoint=checkpoint,
                    lease_as_of=lease_as_of,
                    file_reference_checker=file_reference_checker,
                )
        except RehearsalInjectedFailure:
            pass
        except RehearsalVerificationError:
            raise
        else:
            raise RehearsalVerificationError(f"failure probe did not fail at {checkpoint}")
        if _target_database_state(target_engine) != baseline:
            raise RehearsalVerificationError(f"target database state changed after rollback probe {checkpoint}")
    with target_engine.begin() as target:
        report, mappings = _copy_authoring_slice(
            source,
            target,
            source_revision=source_revision,
            target_revision=target_revision,
            active_revision=active_revision,
            lease_as_of=lease_as_of,
            file_reference_checker=file_reference_checker,
        )
    return AuthoringJobCatalogRehearsalResult(
        installation_id=installation_id,
        source_tables=AUTHORING_JOB_SOURCE_TABLES,
        target_tables=AUTHORING_JOB_TARGET_TABLES,
        excluded_source_tables=(
            "knowledge_import_jobs",
            "knowledge_import_events",
            "analytics_query_results",
            "knowledge_table_assets",
        ),
        source_to_target=mappings,
        report=replace(report, injected_failure_checkpoints=checkpoints),
    )
