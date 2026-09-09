"""Executable Phase 0B rehearsal for structured/table asset metadata."""

from __future__ import annotations

import hashlib
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
from .models import KnowledgeStructuredAsset
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

STRUCTURED_ASSET_SOURCE_TABLES = ("knowledge_table_assets",)
STRUCTURED_ASSET_TARGET_TABLES = ("knowledge_structured_assets",)
STRUCTURED_ASSET_FAILURE_CHECKPOINTS = frozenset({"after_schema", "after_structured_assets", "before_verification"})
DEFAULT_STRUCTURED_ASSET_FAILURE_PROBES = (
    "after_schema",
    "after_structured_assets",
    "before_verification",
)
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_PROFILE_STATUSES = {"missing", "ready", "stale", "processing", "error", "failed"}
_REFERENCE_STATUSES = {"pending", "ready", "removed"}


def _validate_identifier(value: Any, *, label: str, max_length: int = 160) -> str:
    text = str(value or "")
    if len(text) > max_length or not _SAFE_IDENTIFIER.fullmatch(text):
        raise RehearsalVerificationError(f"unsafe structured-asset {label}")
    return text


def _space_id(value: Any) -> str:
    return f"space_{value}"


def _structured_asset_id(value: Any) -> str:
    return f"structured_{value}"


def _asset_id(document_id: Any, source_revision: str) -> str:
    revision_digest = hashlib.sha256(source_revision.encode("utf-8")).hexdigest()[:12]
    return f"asset_{document_id}_{revision_digest}"


def _digest_or_empty(value: Any) -> str:
    text = str(value or "")
    return _source_ref_digest(text) if text else ""


def _content_digest(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    return text if text.startswith("sha256:") else f"sha256:{text}"


def _safe_text(value: Any) -> str:
    sanitized = _sanitize_path_value(str(value or ""))
    if isinstance(sanitized, str):
        return sanitized
    return json.dumps(sanitized, ensure_ascii=False, sort_keys=True)


def _safe_json(value: Any) -> Any:
    return _json_safe(_sanitize_path_value(value if value is not None else {}))


def _source_rows(source: Connection) -> list[dict[str, Any]]:
    return _reflect_rows(
        source,
        "knowledge_table_assets",
        (
            "asset_id",
            "knowledge_base_id",
            "document_id",
            "source_type",
            "file_name",
            "storage_path",
            "virtual_path",
            "sheet_name",
            "size_bytes",
            "modified_at",
            "content_sha256",
            "profile_status",
            "profile_path",
            "rows",
            "columns_count",
            "columns",
            "reference_status",
            "asset_metadata",
            "created_at",
            "updated_at",
        ),
    )


def _validate_source_relationships(source: Connection, rows: Sequence[Mapping[str, Any]]) -> None:
    bases = _reflect_rows(source, "knowledge_bases", ("id",))
    base_ids = {str(row["id"]) for row in bases}
    documents = _reflect_rows(source, "knowledge_documents", ("id", "knowledge_base_id"))
    document_to_base = {str(row["id"]): str(row["knowledge_base_id"]) for row in documents}
    errors: list[str] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        asset_id = _validate_identifier(row["asset_id"], label="asset id", max_length=149)
        base_id = _validate_identifier(row["knowledge_base_id"], label="knowledge base id", max_length=114)
        if base_id not in base_ids:
            errors.append(f"asset {asset_id}: missing knowledge base")
        key = (base_id, asset_id)
        if key in seen:
            errors.append(f"asset {asset_id}: duplicate source identity")
        seen.add(key)
        document_id = row.get("document_id")
        if document_id:
            _validate_identifier(document_id, label="document id", max_length=141)
        if document_id and document_to_base.get(str(document_id)) != base_id:
            errors.append(f"asset {asset_id}: document/base mismatch")
        if str(row.get("profile_status") or "missing") not in _PROFILE_STATUSES:
            errors.append(f"asset {asset_id}: unknown profile status")
        if str(row.get("reference_status") or "pending") not in _REFERENCE_STATUSES:
            errors.append(f"asset {asset_id}: unknown reference status")
        size_bytes = int(row.get("size_bytes") or 0)
        rows_count = row.get("rows")
        columns_count = row.get("columns_count")
        if (
            size_bytes < 0
            or (rows_count is not None and int(rows_count) < 0)
            or (columns_count is not None and int(columns_count) < 0)
        ):
            errors.append(f"asset {asset_id}: negative size/profile count")
        columns = row.get("columns") or []
        if not isinstance(columns, list):
            errors.append(f"asset {asset_id}: columns must be a list")
        elif columns_count is not None and int(columns_count) != len(columns):
            errors.append(f"asset {asset_id}: columns_count mismatch")
    if errors:
        raise RehearsalVerificationError("source structured-asset validation failed: " + "; ".join(errors))


def _canonical_row(row: Mapping[str, Any], *, source_revision: str) -> dict[str, Any]:
    source_asset_id = _validate_identifier(row["asset_id"], label="asset id", max_length=149)
    base_id = _validate_identifier(row["knowledge_base_id"], label="knowledge base id", max_length=114)
    target_id = _structured_asset_id(source_asset_id)
    space_id = _space_id(base_id)
    document_id = row.get("document_id")
    document_asset_id = _asset_id(document_id, source_revision) if document_id else None
    profile_path = str(row.get("profile_path") or "")
    return {
        "id": target_id,
        "space_id": space_id,
        "source_key": source_asset_id,
        "document_asset_id": document_asset_id,
        "source_type": _safe_text(row.get("source_type")),
        "file_name": _safe_text(row.get("file_name")),
        "sheet_name": _safe_text(row.get("sheet_name")) if row.get("sheet_name") else None,
        "size_bytes": int(row.get("size_bytes") or 0),
        "modified_at": row.get("modified_at"),
        "source_uri": f"knowledge://spaces/{space_id}/structured-assets/{target_id}/source",
        "source_reference_digest": _digest_or_empty(row.get("storage_path")),
        "logical_path_digest": _digest_or_empty(row.get("virtual_path")),
        "profile_uri": (f"knowledge://spaces/{space_id}/structured-assets/{target_id}/profile" if profile_path else ""),
        "profile_reference_digest": _digest_or_empty(profile_path),
        "content_digest": _content_digest(row.get("content_sha256")),
        "profile_status": str(row.get("profile_status") or "missing"),
        "row_count": int(row["rows"]) if row.get("rows") is not None else None,
        "column_count": int(row["columns_count"]) if row.get("columns_count") is not None else None,
        "columns_json": [_safe_text(column) for column in (row.get("columns") or [])],
        "reference_status": str(row.get("reference_status") or "pending"),
        "capabilities": ["table_query"],
        "metadata_json": _safe_json(row.get("asset_metadata") or {}),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
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


def _copy_structured_asset_slice(
    source: Connection,
    target: Connection,
    *,
    source_revision: str,
    target_revision: str,
    active_revision: str,
    failure_checkpoint: str | None = None,
    file_reference_checker: Callable[[str], bool] | None = None,
) -> tuple[RehearsalReport, tuple[tuple[str, str, str], ...]]:
    if failure_checkpoint is not None and failure_checkpoint not in STRUCTURED_ASSET_FAILURE_CHECKPOINTS:
        raise ValueError(f"unsupported structured-asset failure checkpoint: {failure_checkpoint}")
    rows = _source_rows(source)
    _validate_identifier(source_revision, label="source revision")
    _validate_source_relationships(source, rows)
    physical_references = sorted(
        {
            reference
            for row in rows
            for reference in _physical_path_references((row.get("storage_path"), row.get("profile_path")))
        }
    )
    if physical_references and file_reference_checker is None:
        raise RehearsalVerificationError(
            "file_reference_checker is required when structured assets contain physical references"
        )
    file_reachable = not physical_references or all(
        file_reference_checker(reference) for reference in physical_references
    )
    if not file_reachable:
        raise RehearsalVerificationError("structured-asset source file-reference reachability check failed")
    canonical_rows = [_canonical_row(row, source_revision=source_revision) for row in rows]
    migrate_to_latest(target)
    schema_ready = inspect(target).has_table(KnowledgeStructuredAsset.__table__.name)
    if failure_checkpoint == "after_schema":
        raise RehearsalInjectedFailure("injected structured-asset rehearsal failure at after_schema")
    table = KnowledgeStructuredAsset.__table__
    for row in canonical_rows:
        _upsert_immutable(target, table, row, ("id",))
    if failure_checkpoint == "after_structured_assets":
        raise RehearsalInjectedFailure("injected structured-asset rehearsal failure at after_structured_assets")
    for row in canonical_rows:
        _upsert_immutable(target, table, row, ("id",))
    if failure_checkpoint == "before_verification":
        raise RehearsalInjectedFailure("injected structured-asset rehearsal failure at before_verification")
    target_rows = _table_snapshot_rows(
        target,
        table,
        fields=tuple(table.c.keys()),
        primary_key_fields=("id",),
        expected_primary_keys=tuple((row["id"],) for row in canonical_rows),
    )
    source_snapshot_rows = [_json_safe(row) for row in canonical_rows]
    target_out_of_scope = _safe_out_of_scope_snapshot(target, table, tuple((row["id"],) for row in canonical_rows))
    source_snapshot = build_table_snapshot("structured_assets", source_snapshot_rows, primary_key_fields=("id",))
    target_snapshot = build_table_snapshot("structured_assets", target_rows, primary_key_fields=("id",))
    secret_values = [
        row[field]
        for row in canonical_rows
        for field in ("source_type", "file_name", "sheet_name", "columns_json", "metadata_json")
    ]
    report = RehearsalReport(
        source_revision=source_revision,
        target_revision=target_revision,
        active_revision_before=active_revision,
        active_revision_after=active_revision,
        source_tables=(source_snapshot,),
        target_tables=(target_snapshot,),
        target_out_of_scope_tables=(target_out_of_scope,) if target_out_of_scope else (),
        retry_idempotent=True,
        checks={
            "secret_redaction": all(_secrets_are_redacted(value) for value in secret_values),
            "file_reachability": file_reachable,
            "lease_state": True,
            "foreign_keys": True,
            "schema_preflight": schema_ready,
        },
        check_scopes={
            "secret_redaction": "structured asset metadata, columns and derived profile metadata",
            "file_reachability": "physical storage/profile paths checked by the explicit file_reference_checker",
            "lease_state": "structured asset metadata is not a lease-bearing table; processing jobs are a later slice",
            "foreign_keys": "legacy base/document ownership is checked; target links use stable IDs without cross-owner FKs",
            "schema_preflight": "v4 schema and migration history are prepared before the data transaction; SQLite DDL rollback is not claimed by the after_schema probe",
        },
    )
    report.verify_safe()
    mappings = tuple(
        (
            "table_asset",
            str(row["asset_id"]),
            f"knowledge://spaces/{_space_id(row['knowledge_base_id'])}/structured-assets/{_structured_asset_id(row['asset_id'])}",
        )
        for row in rows
    )
    return report, mappings


@dataclass(frozen=True, slots=True)
class StructuredAssetCatalogRehearsalResult:
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


def run_structured_asset_catalog_rehearsal_with_rollback_probes(
    source: Connection,
    target_engine: Engine,
    *,
    installation_id: str,
    source_revision: str,
    target_revision: str,
    active_revision: str,
    failure_checkpoints: Sequence[str] = DEFAULT_STRUCTURED_ASSET_FAILURE_PROBES,
    file_reference_checker: Callable[[str], bool] | None = None,
) -> StructuredAssetCatalogRehearsalResult:
    for value, label in (
        (installation_id, "installation id"),
        (source_revision, "source revision"),
        (target_revision, "target revision"),
        (active_revision, "active revision"),
    ):
        _validate_identifier(value, label=label)
    checkpoints = tuple(failure_checkpoints)
    unknown = sorted(set(checkpoints) - STRUCTURED_ASSET_FAILURE_CHECKPOINTS)
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
                _copy_structured_asset_slice(
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
        report, mappings = _copy_structured_asset_slice(
            source,
            target,
            source_revision=source_revision,
            target_revision=target_revision,
            active_revision=active_revision,
            file_reference_checker=file_reference_checker,
        )
    return StructuredAssetCatalogRehearsalResult(
        installation_id=installation_id,
        source_tables=STRUCTURED_ASSET_SOURCE_TABLES,
        target_tables=STRUCTURED_ASSET_TARGET_TABLES,
        excluded_source_tables=(
            "knowledge_bases",
            "knowledge_documents",
            "knowledge_import_jobs",
            "knowledge_import_events",
            "analytics_query_results",
            "semantic_dimension_build_jobs",
            "semantic_dimension_build_events",
        ),
        source_to_target=mappings,
        report=replace(report, injected_failure_checkpoints=checkpoints),
    )
