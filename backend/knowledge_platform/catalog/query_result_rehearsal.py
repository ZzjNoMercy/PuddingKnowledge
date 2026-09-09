"""Executable Phase 0B rehearsal for the generic QueryResult catalog."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, inspect
from sqlalchemy.engine import Engine

from .connector_rehearsal import _physical_path_references, _sanitize_path_value
from .migrations import migrate_to_latest
from .models import KnowledgeQueryResult
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

QUERY_RESULT_SOURCE_TABLES = ("analytics_query_results",)
QUERY_RESULT_TARGET_TABLES = ("knowledge_query_results",)
QUERY_RESULT_FAILURE_CHECKPOINTS = frozenset({"after_schema", "after_query_results", "before_verification"})
DEFAULT_QUERY_RESULT_FAILURE_PROBES = ("after_schema", "after_query_results", "before_verification")
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_STATUSES = {"ready", "expired", "failed"}


def _validate_identifier(value: Any, *, label: str) -> str:
    text = str(value or "")
    if not _SAFE_IDENTIFIER.fullmatch(text):
        raise RehearsalVerificationError(f"unsafe query-result {label}")
    return text


def _query_result_id(value: Any) -> str:
    return f"query_result_{value}"


def _digest_or_empty(value: Any) -> str:
    text = str(value or "")
    return _source_ref_digest(text) if text else ""


def _safe_text(value: Any) -> str:
    sanitized = _sanitize_path_value(str(value or ""))
    return sanitized if isinstance(sanitized, str) else json.dumps(sanitized, ensure_ascii=False, sort_keys=True)


def _safe_profile(value: Any) -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, child in value.items():
            key_text = str(key)
            if key_text.lower() in {"session_id", "tool_call_id", "trace_id"}:
                result[key_text + "_digest"] = _digest_or_empty(child)
            else:
                result[key_text] = _safe_profile(child)
        return _json_safe(_sanitize_path_value(result))
    if isinstance(value, (list, tuple)):
        return [_safe_profile(child) for child in value]
    return _sanitize_path_value(value)


def _source_rows(source: Connection) -> list[dict[str, Any]]:
    return _reflect_rows(
        source,
        "analytics_query_results",
        (
            "id",
            "session_id",
            "tool_call_id",
            "question",
            "sql",
            "columns",
            "row_count",
            "profile_json",
            "artifact_path",
            "artifact_format",
            "status",
            "created_at",
            "expires_at",
        ),
    )


def _validate_rows(rows: Sequence[Mapping[str, Any]]) -> None:
    seen: set[str] = set()
    for row in rows:
        source_id = _validate_identifier(row["id"], label="id")
        if source_id in seen:
            raise RehearsalVerificationError(f"duplicate query-result id: {source_id}")
        seen.add(source_id)
        status = str(row.get("status") or "ready")
        if status not in _STATUSES:
            raise RehearsalVerificationError(f"query-result {source_id}: unknown status")
        if int(row.get("row_count") or 0) < 0:
            raise RehearsalVerificationError(f"query-result {source_id}: negative row count")
        columns = row.get("columns") or []
        if not isinstance(columns, list):
            raise RehearsalVerificationError(f"query-result {source_id}: columns must be a list")
        artifact_format = str(row.get("artifact_format") or "jsonl")
        if not artifact_format or len(artifact_format) > 60:
            raise RehearsalVerificationError(f"query-result {source_id}: invalid artifact format")


def _canonical_row(row: Mapping[str, Any]) -> dict[str, Any]:
    source_id = _validate_identifier(row["id"], label="id")
    target_id = _query_result_id(source_id)
    artifact_path = str(row.get("artifact_path") or "")
    return {
        "id": target_id,
        "status": str(row.get("status") or "ready"),
        "question": _safe_text(row.get("question")),
        "sql_digest": _digest_or_empty(row.get("sql")),
        "columns_json": [_safe_text(column) for column in (row.get("columns") or [])],
        "row_count": int(row.get("row_count") or 0),
        "profile_json": _safe_profile(row.get("profile_json") or {}),
        "artifact_uri": f"knowledge://query-results/{target_id}/artifact" if artifact_path else "",
        "artifact_reference_digest": _digest_or_empty(artifact_path),
        "artifact_format": str(row.get("artifact_format") or "jsonl"),
        "correlation_json": {
            "session_id_digest": _digest_or_empty(row.get("session_id")),
            "tool_call_id_digest": _digest_or_empty(row.get("tool_call_id")),
        },
        "created_at": row.get("created_at"),
        "expires_at": row.get("expires_at"),
    }


def _safe_out_of_scope_snapshot(target: Connection, table: Any, expected: Sequence[tuple[Any, ...]]) -> Any:
    rows = _table_snapshot_rows(
        target, table, fields=tuple(table.c.keys()), primary_key_fields=("id",), expected_primary_keys=None
    )
    expected_keys = set(expected)
    extras = [row for row in rows if (row.get("id"),) not in expected_keys]
    if not extras:
        return None
    return build_table_snapshot(
        f"{table.name}:out_of_scope",
        [_json_safe(_sanitize_path_value(row)) for row in extras],
        primary_key_fields=("id",),
    )


def _copy_query_result_slice(
    source: Connection,
    target: Connection,
    *,
    source_revision: str,
    target_revision: str,
    active_revision: str,
    failure_checkpoint: str | None = None,
    file_reference_checker: Callable[[str], bool] | None = None,
) -> tuple[RehearsalReport, tuple[tuple[str, str, str], ...]]:
    if failure_checkpoint is not None and failure_checkpoint not in QUERY_RESULT_FAILURE_CHECKPOINTS:
        raise ValueError(f"unsupported query-result failure checkpoint: {failure_checkpoint}")
    rows = _source_rows(source)
    _validate_identifier(source_revision, label="source revision")
    _validate_rows(rows)
    physical_references = sorted(
        {reference for row in rows for reference in _physical_path_references((row.get("artifact_path"),))}
    )
    if physical_references and file_reference_checker is None:
        raise RehearsalVerificationError("file_reference_checker is required for query-result artifacts")
    file_reachable = not physical_references or all(
        file_reference_checker(reference) for reference in physical_references
    )
    if not file_reachable:
        raise RehearsalVerificationError("query-result artifact reachability check failed")
    canonical_rows = [_canonical_row(row) for row in rows]
    migrate_to_latest(target)
    schema_ready = inspect(target).has_table(KnowledgeQueryResult.__table__.name)
    if failure_checkpoint == "after_schema":
        raise RehearsalInjectedFailure("injected query-result rehearsal failure at after_schema")
    table = KnowledgeQueryResult.__table__
    for row in canonical_rows:
        _upsert_immutable(target, table, row, ("id",))
    if failure_checkpoint == "after_query_results":
        raise RehearsalInjectedFailure("injected query-result rehearsal failure at after_query_results")
    for row in canonical_rows:
        _upsert_immutable(target, table, row, ("id",))
    if failure_checkpoint == "before_verification":
        raise RehearsalInjectedFailure("injected query-result rehearsal failure at before_verification")
    target_rows = _table_snapshot_rows(
        target,
        table,
        fields=tuple(table.c.keys()),
        primary_key_fields=("id",),
        expected_primary_keys=tuple((row["id"],) for row in canonical_rows),
    )
    source_snapshot = build_table_snapshot(
        "query_results", [_json_safe(row) for row in canonical_rows], primary_key_fields=("id",)
    )
    target_snapshot = build_table_snapshot("query_results", target_rows, primary_key_fields=("id",))
    out_of_scope = _safe_out_of_scope_snapshot(target, table, tuple((row["id"],) for row in canonical_rows))
    secret_values = [row[field] for row in canonical_rows for field in ("question", "profile_json", "correlation_json")]
    report = RehearsalReport(
        source_revision=source_revision,
        target_revision=target_revision,
        active_revision_before=active_revision,
        active_revision_after=active_revision,
        source_tables=(source_snapshot,),
        target_tables=(target_snapshot,),
        target_out_of_scope_tables=(out_of_scope,) if out_of_scope else (),
        retry_idempotent=True,
        checks={
            "secret_redaction": all(_secrets_are_redacted(value) for value in secret_values),
            "file_reachability": file_reachable,
            "lease_state": True,
            "foreign_keys": True,
            "schema_preflight": schema_ready,
        },
        check_scopes={
            "secret_redaction": "question, SQL/profile metadata, correlation IDs and artifact references",
            "file_reachability": "physical query-result artifact paths checked by the explicit checker",
            "lease_state": "query results are immutable result metadata and do not own worker leases",
            "foreign_keys": "source result has no required Catalog FK; session/tool-call IDs are correlation digests only",
            "schema_preflight": "v5 schema/history are prepared before the data transaction; SQLite DDL rollback is not claimed by after_schema",
        },
    )
    report.verify_safe()
    mappings = tuple(
        (
            "analytics_query_result",
            str(row["id"]),
            f"knowledge://query-results/{_query_result_id(row['id'])}",
        )
        for row in rows
    )
    return report, mappings


@dataclass(frozen=True, slots=True)
class QueryResultCatalogRehearsalResult:
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


def run_query_result_catalog_rehearsal_with_rollback_probes(
    source: Connection,
    target_engine: Engine,
    *,
    installation_id: str,
    source_revision: str,
    target_revision: str,
    active_revision: str,
    failure_checkpoints: Sequence[str] = DEFAULT_QUERY_RESULT_FAILURE_PROBES,
    file_reference_checker: Callable[[str], bool] | None = None,
) -> QueryResultCatalogRehearsalResult:
    for value, label in (
        (installation_id, "installation id"),
        (source_revision, "source revision"),
        (target_revision, "target revision"),
        (active_revision, "active revision"),
    ):
        _validate_identifier(value, label=label)
    checkpoints = tuple(failure_checkpoints)
    unknown = sorted(set(checkpoints) - QUERY_RESULT_FAILURE_CHECKPOINTS)
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
                _copy_query_result_slice(
                    source,
                    target,
                    source_revision=source_revision,
                    target_revision=f"{target_revision}-probe-{checkpoint}",
                    active_revision=active_revision,
                    failure_checkpoint=checkpoint,
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
        report, mappings = _copy_query_result_slice(
            source,
            target,
            source_revision=source_revision,
            target_revision=target_revision,
            active_revision=active_revision,
            file_reference_checker=file_reference_checker,
        )
    return QueryResultCatalogRehearsalResult(
        installation_id=installation_id,
        source_tables=QUERY_RESULT_SOURCE_TABLES,
        target_tables=QUERY_RESULT_TARGET_TABLES,
        excluded_source_tables=(
            "knowledge_bases",
            "knowledge_documents",
            "knowledge_table_assets",
            "knowledge_import_jobs",
            "knowledge_import_events",
            "semantic_dimension_build_jobs",
            "semantic_dimension_build_events",
            "worker_access_logs",
        ),
        source_to_target=mappings,
        report=replace(report, injected_failure_checkpoints=checkpoints),
    )
