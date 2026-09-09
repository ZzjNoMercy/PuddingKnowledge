"""Executable Phase 0B rehearsal for the generic Processing Job Catalog."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, inspect
from sqlalchemy.engine import Engine

from .connector_rehearsal import _as_utc, _physical_path_references, _sanitize_path_value
from .migrations import migrate_to_latest
from .models import KnowledgeProcessingEvent, KnowledgeProcessingJob
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

PROCESSING_JOB_SOURCE_TABLES = ("knowledge_import_jobs", "knowledge_import_events")
PROCESSING_JOB_TARGET_TABLES = ("knowledge_processing_jobs", "knowledge_processing_events")
PROCESSING_JOB_FAILURE_CHECKPOINTS = frozenset(
    {"after_schema", "after_processing_jobs", "after_processing_events", "before_verification"}
)
DEFAULT_PROCESSING_JOB_FAILURE_PROBES = (
    "after_schema",
    "after_processing_jobs",
    "after_processing_events",
    "before_verification",
)
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_JOB_STATUSES = {"queued", "staged", "running", "succeeded", "failed", "cancelled"}
_TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}


def _validate_identifier(value: Any, *, label: str) -> str:
    text = str(value or "")
    if not _SAFE_IDENTIFIER.fullmatch(text):
        raise RehearsalVerificationError(f"unsafe processing-job {label}")
    return text


def _job_id(value: Any) -> str:
    return f"processing_{value}"


def _event_id(value: Any) -> str:
    return f"processing_event_{value}"


def _space_id(value: Any) -> str:
    return f"space_{value}"


def _asset_id(value: Any, source_revision: str) -> str:
    revision_digest = hashlib.sha256(source_revision.encode("utf-8")).hexdigest()[:12]
    return f"asset_{value}_{revision_digest}"


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
        "knowledge_import_jobs",
        (
            "id",
            "knowledge_base_id",
            "status",
            "file_name",
            "file_type",
            "file_size",
            "source_path",
            "source_sha256",
            "title",
            "publish_targets",
            "current_step",
            "progress",
            "document_id",
            "source_connection_id",
            "source_item_id",
            "sync_run_id",
            "error_message",
            "retry_count",
            "job_metadata",
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
        "knowledge_import_events",
        ("id", "job_id", "level", "message", "event_metadata", "created_at"),
    )
    return jobs, events


def _validate_leases(jobs: Sequence[Mapping[str, Any]], *, as_of: datetime | None) -> bool:
    as_of_utc = _as_utc(as_of)
    for row in jobs:
        status = str(row.get("status") or "queued")
        if status not in _JOB_STATUSES:
            return False
        if not 0 <= int(row.get("progress") or 0) <= 100 or int(row.get("attempt") or 0) < 0:
            return False
        owner = str(row.get("lease_owner") or "")
        expiry = _as_utc(row.get("lease_expires_at"))
        heartbeat = _as_utc(row.get("heartbeat_at"))
        if status == "running":
            if not owner or expiry is None or heartbeat is None or as_of_utc is None:
                return False
            if heartbeat > expiry or expiry < as_of_utc or heartbeat > as_of_utc:
                return False
        elif status in {"queued", "staged"} | _TERMINAL_STATUSES:
            if owner or expiry is not None or heartbeat is not None:
                return False
        elif owner and expiry is None:
            return False
    return not any(str(row.get("status") or "queued") == "running" for row in jobs) or as_of_utc is not None


def _validate_relationships(
    source: Connection, jobs: Sequence[Mapping[str, Any]], events: Sequence[Mapping[str, Any]]
) -> None:
    bases = _reflect_rows(source, "knowledge_bases", ("id",))
    base_ids = {str(row["id"]) for row in bases}
    documents = _reflect_rows(source, "knowledge_documents", ("id", "knowledge_base_id"))
    document_to_base = {str(row["id"]): str(row["knowledge_base_id"]) for row in documents}
    connections = _reflect_rows(source, "knowledge_source_connections", ("id", "knowledge_base_id"))
    connection_to_base = {str(row["id"]): str(row["knowledge_base_id"]) for row in connections}
    items = _reflect_rows(source, "knowledge_source_items", ("id", "knowledge_base_id", "source_connection_id"))
    item_by_id = {str(row["id"]): row for row in items}
    syncs = _reflect_rows(source, "knowledge_sync_runs", ("id", "source_connection_id"))
    sync_to_connection = {str(row["id"]): str(row["source_connection_id"]) for row in syncs}
    job_ids = {str(row["id"]) for row in jobs}
    errors: list[str] = []
    for row in jobs:
        job = str(row["id"])
        base_id = str(row["knowledge_base_id"])
        if base_id not in base_ids:
            errors.append(f"job {job}: missing knowledge base")
        document_id = row.get("document_id")
        if document_id and document_to_base.get(str(document_id)) != base_id:
            errors.append(f"job {job}: document/base mismatch")
        connection_id = row.get("source_connection_id")
        if connection_id and connection_to_base.get(str(connection_id)) != base_id:
            errors.append(f"job {job}: connector/base mismatch")
        item_id = row.get("source_item_id")
        if item_id:
            item = item_by_id.get(str(item_id))
            if item is None or str(item["knowledge_base_id"]) != base_id:
                errors.append(f"job {job}: source-item/base mismatch")
            elif connection_id and str(item["source_connection_id"]) != str(connection_id):
                errors.append(f"job {job}: source-item/connector mismatch")
        sync_id = row.get("sync_run_id")
        if sync_id and (
            str(sync_id) not in sync_to_connection
            or (connection_id and sync_to_connection[str(sync_id)] != str(connection_id))
        ):
            errors.append(f"job {job}: sync-run/connector mismatch")
    for row in events:
        if str(row["job_id"]) not in job_ids:
            errors.append(f"event {row['id']}: missing import job")
    if errors:
        raise RehearsalVerificationError("source processing-job relationship check failed: " + "; ".join(errors))


def _canonical_job(row: Mapping[str, Any], *, source_revision: str) -> dict[str, Any]:
    source_id = _validate_identifier(row["id"], label="id")
    base_id = _validate_identifier(row["knowledge_base_id"], label="knowledge base id")
    document_id = row.get("document_id")
    metadata = row.get("job_metadata") or {}
    status = str(row.get("status") or "queued")
    lease_owner = _safe_text(row.get("lease_owner")) if row.get("lease_owner") else None
    lease_expires_at = row.get("lease_expires_at")
    heartbeat_at = row.get("heartbeat_at")
    if status in _TERMINAL_STATUSES | {"queued", "staged"}:
        lease_owner = None
        lease_expires_at = None
        heartbeat_at = None
    return {
        "id": _job_id(source_id),
        "space_id": _space_id(base_id),
        "kind": str(metadata.get("kind") or "import"),
        "status": status,
        "title": _safe_text(row.get("title")),
        "file_name": _safe_text(row.get("file_name")),
        "file_type": str(row.get("file_type") or "file"),
        "file_size": int(row.get("file_size") or 0),
        "input_uri": f"knowledge://spaces/{_space_id(base_id)}/processing-jobs/{_job_id(source_id)}/input",
        "input_reference_digest": _digest_or_empty(row.get("source_path")),
        "source_sha256": str(row.get("source_sha256") or ""),
        "asset_id": _asset_id(document_id, source_revision) if document_id else None,
        "source_item_id": f"source_item_{row['source_item_id']}" if row.get("source_item_id") else None,
        "sync_run_id": f"sync_{row['sync_run_id']}" if row.get("sync_run_id") else None,
        "current_step": str(row.get("current_step") or "queued"),
        "progress": int(row.get("progress") or 0),
        "error_message": _safe_text(row.get("error_message")),
        "retry_count": int(row.get("retry_count") or 0),
        "metadata_json": _safe_json(metadata),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "started_at": row.get("started_at"),
        "finished_at": row.get("finished_at"),
        "lease_owner": lease_owner,
        "lease_expires_at": lease_expires_at,
        "heartbeat_at": heartbeat_at,
        "attempt": int(row.get("attempt") or 0),
    }


def _canonical_event(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": _event_id(_validate_identifier(row["id"], label="event id")),
        "job_id": _job_id(_validate_identifier(row["job_id"], label="event job id")),
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


def _copy_processing_slice(
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
    if failure_checkpoint is not None and failure_checkpoint not in PROCESSING_JOB_FAILURE_CHECKPOINTS:
        raise ValueError(f"unsupported processing-job failure checkpoint: {failure_checkpoint}")
    jobs, events = _source_rows(source)
    _validate_identifier(source_revision, label="source revision")
    _validate_relationships(source, jobs, events)
    physical_references = sorted({ref for row in jobs for ref in _physical_path_references((row.get("source_path"),))})
    if physical_references and file_reference_checker is None:
        raise RehearsalVerificationError("file_reference_checker is required for processing-job source paths")
    file_reachable = not physical_references or all(file_reference_checker(ref) for ref in physical_references)
    if not file_reachable:
        raise RehearsalVerificationError("processing-job source file-reference reachability check failed")
    canonical_jobs = [_canonical_job(row, source_revision=source_revision) for row in jobs]
    canonical_events = [_canonical_event(row) for row in events]
    migrate_to_latest(target)
    schema_ready = inspect(target).has_table(KnowledgeProcessingJob.__table__.name) and inspect(target).has_table(
        KnowledgeProcessingEvent.__table__.name
    )
    if failure_checkpoint == "after_schema":
        raise RehearsalInjectedFailure("injected processing-job rehearsal failure at after_schema")
    job_table = KnowledgeProcessingJob.__table__
    event_table = KnowledgeProcessingEvent.__table__
    for row in canonical_jobs:
        _upsert_immutable(target, job_table, row, ("id",))
    if failure_checkpoint == "after_processing_jobs":
        raise RehearsalInjectedFailure("injected processing-job rehearsal failure at after_processing_jobs")
    for row in canonical_events:
        _upsert_immutable(target, event_table, row, ("id",))
    if failure_checkpoint == "after_processing_events":
        raise RehearsalInjectedFailure("injected processing-job rehearsal failure at after_processing_events")
    for row in canonical_jobs:
        _upsert_immutable(target, job_table, row, ("id",))
    for row in canonical_events:
        _upsert_immutable(target, event_table, row, ("id",))
    if failure_checkpoint == "before_verification":
        raise RehearsalInjectedFailure("injected processing-job rehearsal failure at before_verification")
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
    source_snapshots = (
        build_table_snapshot(
            "processing_jobs",
            [_json_safe(row) for row in canonical_jobs],
            primary_key_fields=("id",),
            lease_fields=("status", "lease_owner", "lease_expires_at", "heartbeat_at", "attempt"),
        ),
        build_table_snapshot(
            "processing_events", [_json_safe(row) for row in canonical_events], primary_key_fields=("id",)
        ),
    )
    target_snapshots = (
        build_table_snapshot(
            "processing_jobs",
            target_job_rows,
            primary_key_fields=("id",),
            lease_fields=("status", "lease_owner", "lease_expires_at", "heartbeat_at", "attempt"),
        ),
        build_table_snapshot("processing_events", target_event_rows, primary_key_fields=("id",)),
    )
    out_of_scope = tuple(
        snapshot
        for table, expected in (
            (job_table, tuple((row["id"],) for row in canonical_jobs)),
            (event_table, tuple((row["id"],) for row in canonical_events)),
        )
        if (snapshot := _safe_out_of_scope_snapshot(target, table, expected)) is not None
    )
    secret_values = [row[field] for row in canonical_jobs for field in ("title", "error_message", "metadata_json")] + [
        row[field] for row in canonical_events for field in ("message", "metadata_json")
    ]
    report = RehearsalReport(
        source_revision=source_revision,
        target_revision=target_revision,
        active_revision_before=active_revision,
        active_revision_after=active_revision,
        source_tables=source_snapshots,
        target_tables=target_snapshots,
        target_out_of_scope_tables=out_of_scope,
        retry_idempotent=True,
        checks={
            "secret_redaction": all(_secrets_are_redacted(value) for value in secret_values),
            "file_reachability": file_reachable,
            "lease_state": _validate_leases(canonical_jobs, as_of=lease_as_of),
            "foreign_keys": True,
            "schema_preflight": schema_ready,
        },
        check_scopes={
            "secret_redaction": "processing job/event metadata, errors and messages",
            "file_reachability": "physical import source paths checked by the explicit file_reference_checker",
            "lease_state": "queued/staged/running/terminal import-job lease state checked against explicit as_of",
            "foreign_keys": "legacy base/document/connector/source-item/sync-run/event ownership checked before copy",
            "schema_preflight": "v6 schema/history are prepared before the data transaction; SQLite DDL rollback is not claimed by after_schema",
        },
    )
    report.verify_safe()
    mappings = tuple(
        [
            (
                "import_job",
                str(row["id"]),
                f"knowledge://spaces/{_space_id(row['knowledge_base_id'])}/processing-jobs/{_job_id(row['id'])}",
            )
            for row in jobs
        ]
        + [("import_event", str(row["id"]), f"knowledge://processing-events/{_event_id(row['id'])}") for row in events]
    )
    return report, mappings


@dataclass(frozen=True, slots=True)
class ProcessingJobCatalogRehearsalResult:
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


def run_processing_job_catalog_rehearsal_with_rollback_probes(
    source: Connection,
    target_engine: Engine,
    *,
    installation_id: str,
    source_revision: str,
    target_revision: str,
    active_revision: str,
    failure_checkpoints: Sequence[str] = DEFAULT_PROCESSING_JOB_FAILURE_PROBES,
    file_reference_checker: Callable[[str], bool] | None = None,
    lease_as_of: datetime | None = None,
) -> ProcessingJobCatalogRehearsalResult:
    for value, label in (
        (installation_id, "installation id"),
        (source_revision, "source revision"),
        (target_revision, "target revision"),
        (active_revision, "active revision"),
    ):
        _validate_identifier(value, label=label)
    checkpoints = tuple(failure_checkpoints)
    unknown = sorted(set(checkpoints) - PROCESSING_JOB_FAILURE_CHECKPOINTS)
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
                _copy_processing_slice(
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
        report, mappings = _copy_processing_slice(
            source,
            target,
            source_revision=source_revision,
            target_revision=target_revision,
            active_revision=active_revision,
            lease_as_of=lease_as_of,
            file_reference_checker=file_reference_checker,
        )
    return ProcessingJobCatalogRehearsalResult(
        installation_id=installation_id,
        source_tables=PROCESSING_JOB_SOURCE_TABLES,
        target_tables=PROCESSING_JOB_TARGET_TABLES,
        excluded_source_tables=(
            "knowledge_bases",
            "knowledge_documents",
            "knowledge_source_connections",
            "knowledge_source_items",
            "knowledge_sync_runs",
            "read_later_items",
            "knowledge_table_assets",
            "analytics_query_results",
            "semantic_dimension_build_jobs",
            "semantic_dimension_build_events",
        ),
        source_to_target=mappings,
        report=replace(report, injected_failure_checkpoints=checkpoints),
    )
