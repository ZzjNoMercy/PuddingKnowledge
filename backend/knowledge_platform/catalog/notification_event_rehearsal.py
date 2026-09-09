"""Executable Phase 0B rehearsal for the TaskNotification split.

TaskNotification mixes Platform-produced task facts with Harness-owned inbox
state.  The target is an append-only Platform event stream: it has no
``read_at`` column and no Session/Run/Goal foreign keys.  A Harness inbox may
consume these events and maintain its own acknowledgement state.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, inspect
from sqlalchemy.engine import Engine

from knowledge_contracts import NotificationEvent

from .connector_rehearsal import _sanitize_path_value
from .migrations import migrate_to_latest
from .models import KnowledgeNotificationEvent
from .rehearsal import RehearsalReport, RehearsalVerificationError, build_table_snapshot
from .rehearsal_runner import (
    RehearsalInjectedFailure,
    _engines_are_independent,
    _json_safe,
    _reflect_rows,
    _secrets_are_redacted,
    _source_ref_digest,
    _table_snapshot_rows,
    _target_database_state,
    _upsert_immutable,
)

NOTIFICATION_SOURCE_TABLES = ("task_notifications",)
NOTIFICATION_TARGET_TABLES = ("knowledge_notification_events",)
NOTIFICATION_FAILURE_CHECKPOINTS = frozenset(
    {"after_schema", "after_notification_events", "before_verification"}
)
DEFAULT_NOTIFICATION_FAILURE_PROBES = (
    "after_schema",
    "after_notification_events",
    "before_verification",
)
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_SAFE_CATEGORY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,99}$")
_SAFE_PAYLOAD_KEYS = frozenset({"job_id", "dimension_id", "status", "session_id", "query_id"})
_CORRELATION_KEYS = {"session_id", "query_id"}
_JOB_STATUSES = frozenset(
    {
        "queued",
        "running",
        "waiting_for_publish_confirmation",
        "waiting_for_baseline_change_confirmation",
        "published",
        "failed",
        "cancelled",
    }
)


_SUSPICIOUS_ID = re.compile(r"(?:secret|token|password|session|query|sk_live|akia|bearer)", re.IGNORECASE)
_NON_PORTABLE_TEXT = re.compile(
    r"(?:https?://|file:|[A-Za-z]:[\\/]|\\\\|(?:^|[\s(])/(?:[^\s]+)|(?:^|[\s(])~/|"
    r"(?:^|[\s(])[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+(?:[\s;,) ]|$)|"
    r"(?:^|[\s(])(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}(?:[/?#][^\s]*)?|"
    r"(?:session|query)_id\s*=|(?:sk_live|AKIA)[A-Za-z0-9_-]*|\bbearer\s+|"
    r"(?:password|secret|token)\s*[:=])",
    re.IGNORECASE,
)


def _validate(
    value: Any,
    *,
    label: str,
    pattern: re.Pattern[str] = _SAFE_IDENTIFIER,
    max_length: int = 160,
) -> str:
    text = str(value or "").strip()
    if len(text) > max_length or not pattern.fullmatch(text):
        raise RehearsalVerificationError(f"unsafe notification {label}")
    return text


def _safe_display_text(value: Any, *, label: str, max_length: int) -> str:
    text = str(value or "")
    if len(text) > max_length:
        raise RehearsalVerificationError(f"notification {label} is too long")
    if _NON_PORTABLE_TEXT.search(text):
        return json.dumps({"text_digest": _source_ref_digest(text)}, ensure_ascii=False, sort_keys=True)
    sanitized = _sanitize_path_value(text)
    if isinstance(sanitized, str):
        return sanitized
    return json.dumps(sanitized, ensure_ascii=False, sort_keys=True)


def _safe_payload(value: Any, *, source_id: str) -> dict[str, Any]:
    if value in (None, ""):
        return {}
    if not isinstance(value, Mapping):
        raise RehearsalVerificationError(f"notification {source_id}: payload must be an object")
    unknown = sorted(set(str(key) for key in value) - _SAFE_PAYLOAD_KEYS)
    if unknown:
        raise RehearsalVerificationError(f"notification {source_id}: unsupported payload keys: {unknown}")
    result: dict[str, Any] = {}
    for key, raw in value.items():
        key = str(key)
        if key in _CORRELATION_KEYS:
            if raw not in (None, ""):
                result[f"{key}_digest"] = _source_ref_digest(str(raw))
            continue
        if key in {"job_id", "dimension_id"}:
            candidate = _validate(raw, label=f"payload {key}")
            if _SUSPICIOUS_ID.search(candidate):
                raise RehearsalVerificationError(f"notification {source_id}: unsafe payload {key}")
            result[key] = candidate
            continue
        status = str(raw or "").strip()
        if status not in _JOB_STATUSES:
            raise RehearsalVerificationError(f"notification {source_id}: unsupported payload status")
        result[key] = status
    return result


def _source_rows(source: Connection) -> list[dict[str, Any]]:
    return _reflect_rows(
        source,
        "task_notifications",
        (
            "id",
            "category",
            "subject_type",
            "subject_id",
            "title",
            "body",
            "payload",
            "created_at",
            "read_at",
        ),
    )


def _canonical_row(row: Mapping[str, Any]) -> dict[str, Any]:
    source_id = _validate(row["id"], label="id", pattern=_SAFE_IDENTIFIER, max_length=147)
    category = _validate(row["category"], label="category", pattern=_SAFE_CATEGORY)
    subject_type = _validate(row["subject_type"], label="subject type", max_length=120)
    subject_id = _validate(row["subject_id"], label="subject id")
    payload = _safe_payload(row.get("payload"), source_id=source_id)
    event = NotificationEvent(
        event_id=f"notification_{source_id}",
        event_type="task_notification.v1",
        subject_type=subject_type,
        subject_id=subject_id,
        title=_safe_display_text(row.get("title"), label="title", max_length=300),
        body=_safe_display_text(row.get("body"), label="body", max_length=4000),
        payload={
            (f"{key}_digest" if key in {"session_id", "query_id"} else key): value
            for key, value in payload.items()
        },
        occurred_at=str(_json_safe(row.get("created_at"))),
    )
    return {
        "id": event.event_id,
        "event_type": event.event_type,
        "category": category,
        "subject_type": event.subject_type,
        "subject_id": event.subject_id,
        "title": event.title,
        "body": event.body,
        "payload_json": dict(event.payload),
        "created_at": row.get("created_at"),
    }


def _copy_notification_slice(
    source: Connection,
    target: Connection,
    *,
    source_revision: str,
    target_revision: str,
    active_revision: str,
    failure_checkpoint: str | None = None,
) -> tuple[RehearsalReport, tuple[tuple[str, str, str], ...]]:
    rows = _source_rows(source)
    canonical_rows = [_canonical_row(row) for row in rows]
    migrate_to_latest(target)
    if failure_checkpoint == "after_schema":
        raise RehearsalInjectedFailure("injected rehearsal failure at after_schema")
    table = KnowledgeNotificationEvent.__table__
    for row in canonical_rows:
        _upsert_immutable(target, table, row, ("id",))
    if failure_checkpoint == "after_notification_events":
        raise RehearsalInjectedFailure("injected rehearsal failure at after_notification_events")
    replay_rows = _source_rows(source)
    replayed_canonical_rows = [_canonical_row(row) for row in replay_rows]
    retry_idempotent = replayed_canonical_rows == canonical_rows
    if not retry_idempotent:
        raise RehearsalVerificationError("notification replay changed the source snapshot")
    for row in replayed_canonical_rows:
        _upsert_immutable(target, table, row, ("id",))
    if failure_checkpoint == "before_verification":
        raise RehearsalInjectedFailure("injected rehearsal failure at before_verification")

    fields = tuple(canonical_rows[0]) if canonical_rows else tuple(table.c.keys())
    expected_keys = tuple((row["id"],) for row in canonical_rows)
    target_rows = _table_snapshot_rows(
        target,
        table,
        fields=fields,
        primary_key_fields=("id",),
        expected_primary_keys=expected_keys,
    )
    if not canonical_rows:
        target_rows = []
    all_rows = _table_snapshot_rows(
        target, table, fields=tuple(table.c.keys()), primary_key_fields=("id",), expected_primary_keys=None
    )
    out_of_scope_rows = [row for row in all_rows if (row.get("id"),) not in set(expected_keys)]
    out_of_scope = (
        build_table_snapshot(
            "knowledge_notification_events:out_of_scope",
            [_json_safe(_sanitize_path_value(row)) for row in out_of_scope_rows],
            primary_key_fields=("id",),
        )
        if out_of_scope_rows
        else None
    )
    source_snapshot = build_table_snapshot(
        "notification_events",
        [_json_safe(row) for row in canonical_rows],
        primary_key_fields=("id",),
    )
    target_snapshot = build_table_snapshot(
        "notification_events",
        target_rows,
        primary_key_fields=("id",),
    )
    report = RehearsalReport(
        source_revision=source_revision,
        target_revision=target_revision,
        active_revision_before=active_revision,
        active_revision_after=active_revision,
        source_tables=(source_snapshot,),
        target_tables=(target_snapshot,),
        target_out_of_scope_tables=(out_of_scope,) if out_of_scope else (),
        retry_idempotent=retry_idempotent,
        checks={
            "secret_redaction": all(
                _secrets_are_redacted(row["payload_json"])
                and "session_id" not in row["payload_json"]
                and "query_id" not in row["payload_json"]
                for row in canonical_rows
            ),
            "file_reachability": True,
            "lease_state": True,
            "foreign_keys": True,
        },
        check_scopes={
            "secret_redaction": "notification payloads are allowlisted; session/query correlation becomes a digest and read state is not copied",
            "file_reachability": "notification events contain no physical file reference",
            "lease_state": "notification events do not own a worker lease",
            "foreign_keys": "subject IDs are external stable IDs; Harness inbox/read state is intentionally not a Platform FK",
        },
    )
    report.verify_safe()
    mappings = tuple(
        (
            "task_notification",
            str(row["subject_id"]),
            f"knowledge://events/notifications/{row['id']}",
        )
        for row in canonical_rows
    )
    return report, mappings


@dataclass(frozen=True, slots=True)
class NotificationEventCatalogRehearsalResult:
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


def run_notification_event_catalog_rehearsal_with_rollback_probes(
    source: Connection,
    target_engine: Engine,
    *,
    installation_id: str,
    source_revision: str,
    target_revision: str,
    active_revision: str,
    failure_checkpoints: Sequence[str] = DEFAULT_NOTIFICATION_FAILURE_PROBES,
) -> NotificationEventCatalogRehearsalResult:
    for value, label in (
        (installation_id, "installation id"),
        (source_revision, "source revision"),
        (target_revision, "target revision"),
        (active_revision, "active revision"),
    ):
        _validate(value, label=label)
    checkpoints = tuple(failure_checkpoints)
    unknown = sorted(set(checkpoints) - NOTIFICATION_FAILURE_CHECKPOINTS)
    if not checkpoints:
        raise ValueError("failure_checkpoints must not be empty")
    if unknown:
        raise ValueError(f"unsupported failure_checkpoints: {unknown}")
    if not _engines_are_independent(source.engine, target_engine):
        raise RehearsalVerificationError("source and target must use independent engines")
    if "task_notifications" not in set(inspect(source).get_table_names()):
        raise RehearsalVerificationError("source Catalog is missing task_notifications")
    with target_engine.begin() as target:
        migrate_to_latest(target)
    baseline = _target_database_state(target_engine)
    for checkpoint in checkpoints:
        try:
            with target_engine.begin() as target:
                _copy_notification_slice(
                    source,
                    target,
                    source_revision=source_revision,
                    target_revision=f"{target_revision}-probe-{checkpoint}",
                    active_revision=active_revision,
                    failure_checkpoint=checkpoint,
                )
        except RehearsalInjectedFailure:
            pass
        else:
            raise RehearsalVerificationError(f"failure probe did not fail at {checkpoint}")
        if _target_database_state(target_engine) != baseline:
            raise RehearsalVerificationError(f"target database state changed after rollback probe {checkpoint}")
    with target_engine.begin() as target:
        report, mappings = _copy_notification_slice(
            source,
            target,
            source_revision=source_revision,
            target_revision=target_revision,
            active_revision=active_revision,
        )
    return NotificationEventCatalogRehearsalResult(
        installation_id=installation_id,
        source_tables=NOTIFICATION_SOURCE_TABLES,
        target_tables=NOTIFICATION_TARGET_TABLES,
        excluded_source_tables=(
            "knowledge_bases",
            "knowledge_documents",
            "knowledge_source_connections",
            "knowledge_source_items",
            "knowledge_sync_runs",
            "feishu_app_credentials",
            "feishu_user_grants",
            "feishu_oauth_sessions",
            "read_later_items",
            "knowledge_database_sources",
            "knowledge_table_assets",
            "analytics_query_results",
            "knowledge_import_jobs",
            "knowledge_import_events",
            "semantic_dimension_build_jobs",
            "semantic_dimension_build_events",
            "worker_access_logs",
        ),
        source_to_target=mappings,
        report=replace(report, injected_failure_checkpoints=checkpoints),
    )
