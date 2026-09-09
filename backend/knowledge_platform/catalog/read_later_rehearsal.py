"""Executable Phase 0B rehearsal for Read Later capture ownership.

This adapter keeps the useful behavior of the legacy read-later flow
(canonical URL identity, capture metadata, durable job progress) while making
the Platform boundary explicit: URLs become digests, host paths become stable
``knowledge://`` URIs, and job leases are copied as fenced state.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import Connection
from sqlalchemy.engine import Engine

from .connector_rehearsal import (
    _lease_state_is_valid,
    _physical_path_references,
    _sanitize_path_value,
)
from .migrations import migrate_to_latest
from .models import KnowledgeWebCapture
from .rehearsal import RehearsalReport, RehearsalVerificationError, build_table_snapshot
from .rehearsal_runner import (
    RehearsalInjectedFailure,
    _engines_are_independent,
    _json_safe,
    _redact,
    _reflect_rows,
    _secrets_are_redacted,
    _source_ref_digest,
    _table_snapshot_rows,
    _target_database_state,
    _upsert_immutable,
)

READ_LATER_SOURCE_TABLES = ("read_later_items",)
READ_LATER_TARGET_TABLES = ("knowledge_web_captures",)
READ_LATER_FAILURE_CHECKPOINTS = frozenset({"after_schema", "after_captures", "before_verification"})
_TRACKING_QUERY_KEYS = {"fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid", "igshid", "spm", "from"}
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
DEFAULT_READ_LATER_FAILURE_PROBES = (
    "after_schema",
    "after_captures",
    "before_verification",
)


def _space_id(value: Any) -> str:
    return f"space_{value}"


def _capture_id(value: Any) -> str:
    return f"web_capture_{value}"


def _ingestion_job_id(value: Any) -> str:
    return f"ingestion_{value}"


def _ingestion_event_id(value: Any) -> str:
    return f"ingestion_event_{value}"


def _asset_id(document_id: Any, source_revision: str) -> str:
    revision_digest = hashlib.sha256(source_revision.encode("utf-8")).hexdigest()[:12]
    return f"asset_{document_id}_{revision_digest}"


def _digest_or_empty(value: Any) -> str:
    text = str(value or "")
    return _source_ref_digest(text) if text else ""


def _canonicalize_url(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise RehearsalVerificationError("read-later URL must not be empty")
    parsed = urlsplit(raw)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise RehearsalVerificationError("read-later URL must be an HTTP(S) URL")
    try:
        port = parsed.port
    except ValueError as exc:
        raise RehearsalVerificationError("read-later URL has an invalid port") from exc
    default_port = 80 if parsed.scheme.lower() == "http" else 443
    host = parsed.hostname.encode("idna").decode("ascii").lower()
    netloc = host if port in {None, default_port} else f"{host}:{port}"
    query = [
        (key, item)
        for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in _TRACKING_QUERY_KEYS
    ]
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    canonical = urlunsplit((parsed.scheme.lower(), netloc, path, urlencode(sorted(query)), ""))
    if len(canonical.encode("utf-8")) > 1800:
        raise RehearsalVerificationError("read-later canonical URL is too long")
    return canonical


def _validate_capture_urls(captures: Sequence[Mapping[str, Any]]) -> None:
    for row in captures:
        original = str(row.get("original_url") or "")
        canonical = str(row.get("canonical_url") or "")
        if _canonicalize_url(original) != canonical:
            raise RehearsalVerificationError(
                f"read-later capture {row['id']}: canonical_url does not match URL normalization"
            )


def _content_digest(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    return text if text.startswith("sha256:") else f"sha256:{text}"


def _stable_uri(space_id: str, kind: str, source_id: Any, suffix: str = "") -> str:
    tail = f"/{suffix}" if suffix else ""
    return f"knowledge://spaces/{space_id}/{kind}/{source_id}{tail}"


def _validate_identifier(value: Any, *, label: str) -> str:
    text = str(value or "")
    if not _SAFE_IDENTIFIER.fullmatch(text):
        raise RehearsalVerificationError(f"unsafe read-later {label}")
    return text


def _validate_source_identifiers(
    captures: Sequence[Mapping[str, Any]],
    jobs: Sequence[Mapping[str, Any]],
    events: Sequence[Mapping[str, Any]],
    *,
    source_revision: str,
) -> None:
    _validate_identifier(source_revision, label="source revision")
    for label, rows in (("capture", captures), ("job", jobs), ("event", events)):
        for row in rows:
            _validate_identifier(row["id"], label=f"{label} id")
            if label != "event":
                _validate_identifier(row["knowledge_base_id"], label=f"{label} knowledge base id")
            for field in ("document_id", "source_connection_id", "source_item_id", "sync_run_id", "wiki_job_id"):
                if row.get(field):
                    _validate_identifier(row[field], label=f"{label} {field}")
            if label == "job":
                linked_capture = (row.get("job_metadata") or {}).get("read_later_item_id")
                if linked_capture:
                    _validate_identifier(linked_capture, label="job read-later item id")
            if label == "event":
                _validate_identifier(row["job_id"], label="event job id")


def _safe_text(value: Any) -> str:
    sanitized = _sanitize_path_value(str(value or ""))
    return sanitized if isinstance(sanitized, str) else json.dumps(sanitized, ensure_ascii=False, sort_keys=True)


def _safe_json(value: Any) -> Any:
    return _json_safe(_sanitize_path_value(value if value is not None else {}))


def _safe_out_of_scope_snapshot(
    target: Connection,
    table: Any,
    *,
    expected_primary_keys: Sequence[tuple[Any, ...]],
    lease_fields: Sequence[str] = (),
) -> Any:
    rows = _table_snapshot_rows(
        target,
        table,
        fields=tuple(table.c.keys()),
        primary_key_fields=("id",),
        expected_primary_keys=None,
    )
    expected = set(expected_primary_keys)
    extras = [row for row in rows if (row.get("id"),) not in expected]
    if not extras:
        return None
    sanitized = [_json_safe(_sanitize_path_value(row)) for row in extras]
    return build_table_snapshot(
        f"{table.name}:out_of_scope",
        sanitized,
        primary_key_fields=("id",),
        lease_fields=lease_fields,
    )


def _canonical_capture(row: Mapping[str, Any], *, source_revision: str) -> dict[str, Any]:
    capture_id = _capture_id(row["id"])
    space_id = _space_id(row["knowledge_base_id"])
    document_id = row.get("document_id")
    asset_id = _asset_id(document_id, source_revision) if document_id else None
    job_id = _ingestion_job_id(row["wiki_job_id"]) if row.get("wiki_job_id") else None
    return {
        "id": capture_id,
        "space_id": space_id,
        "original_url_digest": _digest_or_empty(row.get("original_url")),
        "canonical_url_digest": _digest_or_empty(row.get("canonical_url")),
        "title": _safe_text(row.get("title")),
        "site_name": _safe_text(row.get("site_name")),
        "author": _safe_text(row.get("author")),
        "description": _safe_text(row.get("description")),
        "image_url_digest": _digest_or_empty(row.get("image_url")),
        "content_uri": _stable_uri(space_id, "captures", capture_id, "content"),
        "raw_snapshot_uri": (
            _stable_uri(space_id, "captures", capture_id, "raw-snapshot") if row.get("raw_snapshot_path") else ""
        ),
        "content_digest": _content_digest(row.get("content_sha256")),
        "parse_status": str(row.get("parse_status") or "queued"),
        "reading_status": str(row.get("reading_status") or "unread"),
        "error_message": _safe_text(row.get("error_message")),
        "tags_json": [_safe_text(tag) for tag in (row.get("tags") or [])],
        "note": _safe_text(row.get("note")),
        "asset_id": asset_id,
        "connector_id": f"connector_{row['source_connection_id']}" if row.get("source_connection_id") else None,
        "source_item_id": f"source_item_{row['source_item_id']}" if row.get("source_item_id") else None,
        "ingestion_job_id": job_id,
        "fetched_at": row.get("fetched_at"),
        "read_at": row.get("read_at"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def _canonical_job(row: Mapping[str, Any], *, source_revision: str) -> dict[str, Any]:
    job_id = _ingestion_job_id(row["id"])
    space_id = _space_id(row["knowledge_base_id"])
    document_id = row.get("document_id")
    metadata = _safe_json(row.get("job_metadata") or {})
    kind = str((row.get("job_metadata") or {}).get("kind") or "import")
    return {
        "id": job_id,
        "space_id": space_id,
        "kind": kind,
        "status": str(row.get("status") or "queued"),
        "file_name": _safe_text(row.get("file_name")),
        "file_type": str(row.get("file_type") or "file"),
        "file_size": int(row.get("file_size") or 0),
        "source_uri": _stable_uri(space_id, "ingestion-jobs", job_id, "source"),
        "source_digest": _digest_or_empty(row.get("source_path")),
        "title": _safe_text(row.get("title")),
        "publish_targets": [_safe_text(target) for target in (row.get("publish_targets") or [])],
        "current_step": str(row.get("current_step") or "queued"),
        "progress": int(row.get("progress") or 0),
        "asset_id": _asset_id(document_id, source_revision) if document_id else None,
        "capture_id": (
            _capture_id((row.get("job_metadata") or {}).get("read_later_item_id"))
            if (row.get("job_metadata") or {}).get("read_later_item_id")
            else None
        ),
        "connector_id": f"connector_{row['source_connection_id']}" if row.get("source_connection_id") else None,
        "source_item_id": f"source_item_{row['source_item_id']}" if row.get("source_item_id") else None,
        "sync_run_id": f"sync_{row['sync_run_id']}" if row.get("sync_run_id") else None,
        "error_message": _safe_text(row.get("error_message")),
        "retry_count": int(row.get("retry_count") or 0),
        "metadata_json": metadata,
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "started_at": row.get("started_at"),
        "finished_at": row.get("finished_at"),
        "lease_owner": _redact(row.get("lease_owner")),
        "lease_expires_at": row.get("lease_expires_at"),
        "heartbeat_at": row.get("heartbeat_at"),
        "attempt": int(row.get("attempt") or 0),
    }


def _canonical_event(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": _ingestion_event_id(row["id"]),
        "job_id": _ingestion_job_id(row["job_id"]),
        "level": str(row.get("level") or "info"),
        "message": _safe_text(row.get("message")),
        "metadata_json": _safe_json(row.get("event_metadata") or {}),
        "created_at": row.get("created_at"),
    }


def _source_rows(source: Connection) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    captures = _reflect_rows(
        source,
        "read_later_items",
        (
            "id",
            "knowledge_base_id",
            "original_url",
            "canonical_url",
            "title",
            "site_name",
            "author",
            "description",
            "image_url",
            "storage_path",
            "virtual_path",
            "content_sha256",
            "parse_status",
            "reading_status",
            "error_message",
            "tags",
            "note",
            "document_id",
            "source_connection_id",
            "source_item_id",
            "raw_snapshot_path",
            "wiki_job_id",
            "fetched_at",
            "read_at",
            "created_at",
            "updated_at",
        ),
    )
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
    return captures, jobs, events


def _verify_source_relationships(
    source: Connection,
    captures: Sequence[Mapping[str, Any]],
    jobs: Sequence[Mapping[str, Any]],
    events: Sequence[Mapping[str, Any]],
) -> None:
    base_rows = _reflect_rows(source, "knowledge_bases", ("id",))
    base_ids = {str(row["id"]) for row in base_rows}
    documents = _reflect_rows(source, "knowledge_documents", ("id", "knowledge_base_id"))
    document_to_base = {str(row["id"]): str(row["knowledge_base_id"]) for row in documents}
    connections = _reflect_rows(source, "knowledge_source_connections", ("id", "knowledge_base_id"))
    connection_to_base = {str(row["id"]): str(row["knowledge_base_id"]) for row in connections}
    items = _reflect_rows(source, "knowledge_source_items", ("id", "knowledge_base_id", "source_connection_id"))
    item_by_id = {str(row["id"]): row for row in items}
    sync_runs = _reflect_rows(source, "knowledge_sync_runs", ("id", "source_connection_id"))
    sync_to_connection = {str(row["id"]): str(row["source_connection_id"]) for row in sync_runs}
    capture_ids = {str(row["id"]) for row in captures}
    job_by_id = {str(row["id"]): row for row in jobs}
    errors: list[str] = []

    def check_common(row: Mapping[str, Any], label: str) -> None:
        row_id = str(row["id"])
        base_id = str(row["knowledge_base_id"])
        if base_id not in base_ids:
            errors.append(f"{label} {row_id}: missing knowledge base")
        document_id = row.get("document_id")
        if document_id and (str(document_id) not in document_to_base or document_to_base[str(document_id)] != base_id):
            errors.append(f"{label} {row_id}: document/base mismatch")
        connection_id = row.get("source_connection_id")
        if connection_id and connection_to_base.get(str(connection_id)) != base_id:
            errors.append(f"{label} {row_id}: connector/base mismatch")
        item_id = row.get("source_item_id")
        if item_id:
            item = item_by_id.get(str(item_id))
            if item is None or str(item["knowledge_base_id"]) != base_id:
                errors.append(f"{label} {row_id}: source-item/base mismatch")
            elif connection_id and str(item["source_connection_id"]) != str(connection_id):
                errors.append(f"{label} {row_id}: source-item/connector mismatch")

    for row in captures:
        check_common(row, "capture")
        wiki_job_id = str(row.get("wiki_job_id") or "")
        if wiki_job_id and wiki_job_id not in job_by_id:
            errors.append(f"capture {row['id']}: missing wiki job")
    for row in jobs:
        check_common(row, "job")
        sync_run_id = row.get("sync_run_id")
        connection_id = row.get("source_connection_id")
        if sync_run_id and (
            str(sync_run_id) not in sync_to_connection
            or (connection_id and sync_to_connection[str(sync_run_id)] != str(connection_id))
        ):
            errors.append(f"job {row['id']}: sync-run/connector mismatch")
        capture_id = str((row.get("job_metadata") or {}).get("read_later_item_id") or "")
        if capture_id and capture_id not in capture_ids:
            errors.append(f"job {row['id']}: missing read-later capture")
    for row in events:
        if str(row["job_id"]) not in job_by_id:
            errors.append(f"event {row['id']}: missing import job")
    if errors:
        raise RehearsalVerificationError("source read-later foreign-key check failed: " + "; ".join(errors))


def _copy_read_later_slice(
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
    if failure_checkpoint is not None and failure_checkpoint not in READ_LATER_FAILURE_CHECKPOINTS:
        raise ValueError(f"unsupported read-later failure checkpoint: {failure_checkpoint}")
    captures, jobs, events = _source_rows(source)
    _validate_source_identifiers(captures, jobs, events, source_revision=source_revision)
    _validate_capture_urls(captures)
    _verify_source_relationships(source, captures, jobs, events)
    physical_references = sorted(
        {
            reference
            for row in captures
            for reference in _physical_path_references((row.get("storage_path"), row.get("raw_snapshot_path")))
        }
        | {reference for row in jobs for reference in _physical_path_references((row.get("source_path"),))}
    )
    if physical_references and file_reference_checker is None:
        raise RehearsalVerificationError(
            "file_reference_checker is required when read-later rows contain physical references"
        )
    file_reachable = not physical_references or all(
        file_reference_checker(reference) for reference in physical_references
    )
    if not file_reachable:
        raise RehearsalVerificationError("read-later source file-reference reachability check failed")
    capture_rows = [_canonical_capture(row, source_revision=source_revision) for row in captures]
    migrate_to_latest(target)
    capture_table = KnowledgeWebCapture.__table__
    if failure_checkpoint == "after_schema":
        raise RehearsalInjectedFailure("injected read-later rehearsal failure at after_schema")
    for row in capture_rows:
        _upsert_immutable(target, capture_table, row, ("id",))
    if failure_checkpoint == "after_captures":
        raise RehearsalInjectedFailure("injected read-later rehearsal failure at after_captures")
    for row in capture_rows:
        _upsert_immutable(target, capture_table, row, ("id",))
    if failure_checkpoint == "before_verification":
        raise RehearsalInjectedFailure("injected read-later rehearsal failure at before_verification")

    target_rows = {
        "web_captures": _table_snapshot_rows(
            target,
            capture_table,
            fields=tuple(capture_table.c.keys()),
            primary_key_fields=("id",),
            expected_primary_keys=tuple((row["id"],) for row in capture_rows),
        ),
    }
    source_rows = {
        "web_captures": [_json_safe(row) for row in capture_rows],
    }
    expected_keys = {
        "web_captures": tuple((row["id"],) for row in capture_rows),
    }
    tables = (("web_captures", capture_table),)
    target_out_of_scope_tables = tuple(
        snapshot
        for name, table in tables
        if (
            snapshot := _safe_out_of_scope_snapshot(
                target,
                table,
                expected_primary_keys=expected_keys[name],
                lease_fields=(),
            )
        )
        is not None
    )
    source_snapshots = tuple(
        build_table_snapshot(name, rows, primary_key_fields=("id",))
        for name, rows in source_rows.items()
    )
    target_snapshots = tuple(
        build_table_snapshot(name, rows, primary_key_fields=("id",))
        for name, rows in target_rows.items()
    )
    secret_values = (
        [row[field] for row in capture_rows for field in ("title", "description", "error_message", "note", "tags_json")]
    )
    report = RehearsalReport(
        source_revision=source_revision,
        target_revision=target_revision,
        active_revision_before=active_revision,
        active_revision_after=active_revision,
        source_tables=source_snapshots,
        target_tables=target_snapshots,
        target_out_of_scope_tables=target_out_of_scope_tables,
        retry_idempotent=True,
        checks={
            "secret_redaction": all(_secrets_are_redacted(value) for value in secret_values),
            "file_reachability": file_reachable,
                "lease_state": _lease_state_is_valid(
                    jobs,
                    as_of=lease_as_of,
                ),
            "foreign_keys": True,
        },
        check_scopes={
            "secret_redaction": "capture titles/notes/errors are sanitized; Processing owns import job/event copy",
            "file_reachability": "physical storage, raw-snapshot and import source paths checked by the explicit checker",
            "lease_state": "ingestion job status, owner, expiry, heartbeat and attempt are fenced against explicit as_of",
            "foreign_keys": "capture links are checked against legacy dependencies; Processing owns job/event persistence",
        },
    )
    report.verify_safe()
    mappings = tuple(
        [
            (
                "read_later_item",
                str(row["id"]),
                _stable_uri(_space_id(row["knowledge_base_id"]), "captures", _capture_id(row["id"])),
            )
            for row in captures
        ]
    )
    return report, mappings


@dataclass(frozen=True, slots=True)
class ReadLaterCatalogRehearsalResult:
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
                {"source_type": source_type, "source_id": source_id, "target_uri": target_uri}
                for source_type, source_id, target_uri in self.source_to_target
            ],
            "report": self.report.to_dict(),
        }

    def write_json(self, path: str | Path) -> None:
        Path(path).write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )


def run_read_later_catalog_rehearsal_with_rollback_probes(
    source: Connection,
    target_engine: Engine,
    *,
    installation_id: str,
    source_revision: str,
    target_revision: str,
    active_revision: str,
    failure_checkpoints: Sequence[str] = DEFAULT_READ_LATER_FAILURE_PROBES,
    file_reference_checker: Callable[[str], bool] | None = None,
    lease_as_of: datetime | None = None,
) -> ReadLaterCatalogRehearsalResult:
    _validate_identifier(installation_id, label="installation id")
    _validate_identifier(source_revision, label="source revision")
    _validate_identifier(target_revision, label="target revision")
    _validate_identifier(active_revision, label="active revision")
    checkpoints = tuple(failure_checkpoints)
    unknown = sorted(set(checkpoints) - READ_LATER_FAILURE_CHECKPOINTS)
    if not checkpoints:
        raise ValueError("failure_checkpoints must not be empty")
    if unknown:
        raise ValueError(f"unsupported failure_checkpoints: {unknown}")
    if not _engines_are_independent(source.engine, target_engine):
        raise RehearsalVerificationError("source and target must use independent engines")
    with target_engine.begin() as target:
        migrate_to_latest(target)
    baseline = _target_database_state(target_engine)
    for checkpoint in checkpoints:
        try:
            with target_engine.begin() as target:
                _copy_read_later_slice(
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
        report, mappings = _copy_read_later_slice(
            source,
            target,
            source_revision=source_revision,
            target_revision=target_revision,
            active_revision=active_revision,
            file_reference_checker=file_reference_checker,
            lease_as_of=lease_as_of,
        )
    return ReadLaterCatalogRehearsalResult(
        installation_id=installation_id,
        source_tables=READ_LATER_SOURCE_TABLES,
        target_tables=READ_LATER_TARGET_TABLES,
        excluded_source_tables=(
            "knowledge_bases",
            "knowledge_documents",
            "knowledge_source_connections",
            "knowledge_source_items",
            "knowledge_sync_runs",
            "feishu_app_credentials",
            "feishu_user_grants",
            "feishu_oauth_sessions",
            "knowledge_database_sources",
            "knowledge_table_assets",
            "analytics_query_results",
            "knowledge_import_jobs",
            "knowledge_import_events",
            "semantic_dimension_build_jobs",
            "semantic_dimension_build_events",
            "worker_access_logs",
            "task_notifications",
        ),
        source_to_target=mappings,
        report=replace(report, injected_failure_checkpoints=checkpoints),
    )
