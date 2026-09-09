"""Executable Phase 0B rehearsal for database-source ownership.

The legacy database-source row mixes connection metadata with a plaintext
password.  This rehearsal reads that table through reflected Core tables and
copies only non-secret metadata plus a safe credential reference into an
independent Platform table.  It does not read, export, or verify secret
bytes; Vault rebind/rotation is a separate capability and remains outside
this database transaction.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, MetaData, Table, func, inspect, select
from sqlalchemy.engine import Engine

from .connector_rehearsal import _credential_reference, _sanitize_path_value
from .migrations import migrate_to_latest
from .models import KnowledgeDatabaseSource, KnowledgeSpace
from .rehearsal import RehearsalReport, RehearsalVerificationError, build_table_snapshot
from .rehearsal_runner import (
    RehearsalInjectedFailure,
    _engines_are_independent,
    _json_safe,
    _reflect_rows,
    _secrets_are_redacted,
    _table_snapshot_rows,
    _target_database_state,
    _upsert_immutable,
)

DATABASE_SOURCE_SOURCE_TABLES = ("knowledge_database_sources",)
DATABASE_SOURCE_TARGET_TABLES = ("knowledge_database_connectors",)
DATABASE_SOURCE_FAILURE_CHECKPOINTS = frozenset(
    {"after_schema", "after_database_sources", "before_verification"}
)
DEFAULT_DATABASE_SOURCE_FAILURE_PROBES = (
    "after_schema",
    "after_database_sources",
    "before_verification",
)
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_SAFE_TABLE_NAME = re.compile(r"^[A-Za-z0-9_$-]{1,128}(?:\.[A-Za-z0-9_$-]{1,128})?$")
_SUPPORTED_TYPES = {"postgresql", "mysql"}
_DEFAULT_PORTS = {"postgresql": 5432, "mysql": 3306}
_CREDENTIAL_PLACEHOLDER = "vault://users/local/credentials/database-source-{}"
_SAFE_METADATA_KEYS = frozenset({"builtin", "schema", "sslmode", "configured_by", "environment_override"})
_SAFE_SSL_MODES = frozenset({"disable", "allow", "prefer", "require", "verify-ca", "verify-full"})
_SAFE_CONFIGURED_BY = frozenset({"config.json", "default", "environment"})


def _validate_identifier(value: Any, *, label: str, max_length: int = 160) -> str:
    text = str(value or "")
    if len(text) > max_length or not _SAFE_IDENTIFIER.fullmatch(text):
        raise RehearsalVerificationError(f"unsafe database-source {label}")
    return text


def _normalize_tables(value: Any, *, source_id: str) -> list[str]:
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise RehearsalVerificationError(f"database source {source_id}: selected_tables must be a list")
    result: list[str] = []
    seen: set[str] = set()
    for raw in value:
        table = str(raw or "").strip()
        if not _SAFE_TABLE_NAME.fullmatch(table):
            raise RehearsalVerificationError(f"database source {source_id}: unsafe selected table")
        if table in seen:
            raise RehearsalVerificationError(f"database source {source_id}: duplicate selected table")
        seen.add(table)
        result.append(table)
    return result


def _drop_credential_refs(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _drop_credential_refs(child)
            for key, child in value.items()
            if str(key).lower() not in {"credential_ref", "password_ref"}
        }
    if isinstance(value, (list, tuple)):
        return [_drop_credential_refs(child) for child in value]
    return value


def _safe_metadata(value: Any) -> dict[str, Any]:
    if value in (None, ""):
        return {}
    if not isinstance(value, Mapping):
        raise RehearsalVerificationError("database source metadata must be an object")
    raw = _drop_credential_refs(value)
    unknown = sorted(set(str(key) for key in raw) - _SAFE_METADATA_KEYS)
    if unknown:
        raise RehearsalVerificationError(f"database source metadata contains unsupported keys: {unknown}")
    if any(isinstance(child, (Mapping, list, tuple)) for child in raw.values()):
        raise RehearsalVerificationError("database source metadata values must be scalar")
    for key, value in raw.items():
        if key in {"builtin", "environment_override"} and not isinstance(value, bool):
            raise RehearsalVerificationError(f"database source metadata {key} must be boolean")
        if key == "schema" and not re.fullmatch(r"[A-Za-z0-9_$-]{1,128}", str(value)):
            raise RehearsalVerificationError("database source metadata schema is unsafe")
        if key == "sslmode" and str(value) not in _SAFE_SSL_MODES:
            raise RehearsalVerificationError("database source metadata sslmode is unsupported")
        if key == "configured_by" and str(value) not in _SAFE_CONFIGURED_BY and not re.fullmatch(
            r"PUDDINGCLAW_[A-Z0-9_]{1,100}", str(value)
        ):
            raise RehearsalVerificationError("database source metadata configured_by is unsafe")
    sanitized = _sanitize_path_value(raw)
    if not isinstance(sanitized, dict):
        raise RehearsalVerificationError("database source metadata must remain an object")
    return _json_safe(sanitized)


def _credential_ref(row: Mapping[str, Any], *, source_id: str) -> tuple[str, bool]:
    metadata = row.get("source_metadata") if isinstance(row.get("source_metadata"), Mapping) else {}
    raw_ref = str(metadata.get("credential_ref") or "").strip()
    safe_ref = _credential_reference(raw_ref) if raw_ref else ""
    if raw_ref and safe_ref == "<redacted>":
        raise RehearsalVerificationError(f"database source {source_id}: unsafe credential reference")
    if raw_ref and raw_ref.rsplit("/", 1)[-1] != f"database-source-{source_id}":
        raise RehearsalVerificationError(f"database source {source_id}: credential reference identity mismatch")
    has_legacy_password = bool(row.get("_has_legacy_password"))
    if safe_ref:
        return safe_ref, has_legacy_password
    if has_legacy_password:
        return _CREDENTIAL_PLACEHOLDER.format(source_id), True
    return "", False


def _source_rows(source: Connection) -> list[dict[str, Any]]:
    table = Table("knowledge_database_sources", MetaData(), autoload_with=source)
    required = (
        "id",
        "knowledge_base_id",
        "source_type",
        "name",
        "description",
        "host",
        "port",
        "database",
        "username",
        "password",
        "selected_tables",
        "source_metadata",
        "created_at",
        "updated_at",
    )
    missing = sorted(set(required) - set(table.c.keys()))
    if missing:
        raise RehearsalVerificationError(f"knowledge_database_sources: required source columns missing: {missing}")
    # Never select the password value into Python.  The database evaluates a
    # boolean existence expression, while the target receives only a
    # deterministic placeholder/ref.
    columns = [table.c[name] for name in required if name != "password"]
    columns.append((func.length(table.c.password) > 0).label("_has_legacy_password"))
    return [dict(row) for row in source.execute(select(*columns)).mappings().all()]


def _validate_relationships(source: Connection, rows: Sequence[Mapping[str, Any]]) -> None:
    base_rows = _reflect_rows(source, "knowledge_bases", ("id",))
    base_ids = {str(row["id"]) for row in base_rows}
    errors: list[str] = []
    seen_ids: set[str] = set()
    seen_names: set[tuple[str, str]] = set()
    for row in rows:
        source_id = _validate_identifier(row["id"], label="source id", max_length=150)
        base_id = _validate_identifier(row["knowledge_base_id"], label="knowledge base id", max_length=110)
        if source_id in seen_ids:
            errors.append(f"source {source_id}: duplicate id")
        seen_ids.add(source_id)
        if base_id not in base_ids:
            errors.append(f"source {source_id}: missing knowledge base")
        name = str(row.get("name") or "").strip()
        if not name or len(name) > 240:
            errors.append(f"source {source_id}: invalid name")
        key = (base_id, name)
        if key in seen_names:
            errors.append(f"source {source_id}: duplicate name in knowledge base")
        seen_names.add(key)
        source_type = str(row.get("source_type") or "postgresql").strip().lower()
        if source_type not in _SUPPORTED_TYPES:
            errors.append(f"source {source_id}: unsupported source type")
        try:
            port = int(row.get("port") or _DEFAULT_PORTS.get(source_type, 0))
        except (TypeError, ValueError):
            port = 0
        if not 1 <= port <= 65535:
            errors.append(f"source {source_id}: invalid port")
        for field, max_length in (("host", 300), ("database", 200), ("username", 200)):
            if len(str(row.get(field) or "").strip()) > max_length:
                errors.append(f"source {source_id}: {field} is too long")
        if not str(row.get("database") or "").strip():
            errors.append(f"source {source_id}: database name is empty")
        try:
            _normalize_tables(row.get("selected_tables"), source_id=source_id)
        except RehearsalVerificationError as exc:
            errors.append(str(exc))
    if errors:
        raise RehearsalVerificationError("source database-source validation failed: " + "; ".join(errors))


def _canonical_row(row: Mapping[str, Any]) -> dict[str, Any]:
    source_id = _validate_identifier(row["id"], label="source id", max_length=150)
    base_id = _validate_identifier(row["knowledge_base_id"], label="knowledge base id", max_length=110)
    source_type = str(row.get("source_type") or "postgresql").strip().lower()
    credential_ref, pending_rebind = _credential_ref(row, source_id=source_id)
    metadata = _safe_metadata(row.get("source_metadata"))
    metadata["migration_state"] = "pending_rebind" if pending_rebind else "not_required"
    return {
        "id": f"dbsource_{source_id}",
        "space_id": f"space_{base_id}",
        "source_key": source_id,
        "source_type": source_type,
        "name": str(row.get("name") or "").strip(),
        "description": str(row.get("description") or "").strip(),
        "host": str(row.get("host") or "127.0.0.1").strip(),
        "port": int(row.get("port") or _DEFAULT_PORTS[source_type]),
        "database_name": str(row.get("database") or "").strip(),
        "username": str(row.get("username") or "").strip(),
        "credential_ref": credential_ref,
        "selected_tables": _normalize_tables(row.get("selected_tables"), source_id=source_id),
        "config_json": metadata,
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def _copy_database_source_slice(
    source: Connection,
    target: Connection,
    *,
    source_revision: str,
    target_revision: str,
    active_revision: str,
    failure_checkpoint: str | None = None,
) -> tuple[RehearsalReport, tuple[tuple[str, str, str], ...]]:
    rows = _source_rows(source)
    _validate_relationships(source, rows)
    canonical_rows = [_canonical_row(row) for row in rows]
    migrate_to_latest(target)
    if failure_checkpoint == "after_schema":
        raise RehearsalInjectedFailure("injected rehearsal failure at after_schema")
    required_space_ids = {str(row["space_id"]) for row in canonical_rows}
    existing_space_ids = {str(value) for value in target.execute(select(KnowledgeSpace.id)).scalars()}
    missing_space_ids = sorted(required_space_ids - existing_space_ids)
    if missing_space_ids:
        raise RehearsalVerificationError(f"target Platform spaces are missing: {missing_space_ids}")
    table = KnowledgeDatabaseSource.__table__
    for row in canonical_rows:
        _upsert_immutable(target, table, row, ("id",))
    if failure_checkpoint == "after_database_sources":
        raise RehearsalInjectedFailure("injected rehearsal failure at after_database_sources")
    replay_rows = _source_rows(source)
    _validate_relationships(source, replay_rows)
    replayed_canonical_rows = [_canonical_row(row) for row in replay_rows]
    retry_idempotent = replayed_canonical_rows == canonical_rows
    if not retry_idempotent:
        raise RehearsalVerificationError("database source replay changed the source snapshot")
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
    out_of_scope_rows = _table_snapshot_rows(
        target,
        table,
        fields=fields,
        primary_key_fields=("id",),
        expected_primary_keys=None,
    )
    expected = {row["id"] for row in canonical_rows}
    out_of_scope_rows = [row for row in out_of_scope_rows if row["id"] not in expected]
    out_of_scope_rows = [_json_safe(_sanitize_path_value(row)) for row in out_of_scope_rows]
    out_of_scope = (
        build_table_snapshot(
            f"{table.name}:out_of_scope",
            out_of_scope_rows,
            primary_key_fields=("id",),
            secret_fields=("credential_ref",),
        )
        if out_of_scope_rows
        else None
    )
    source_snapshot = build_table_snapshot(
        "database_sources",
        [_json_safe(row) for row in canonical_rows],
        primary_key_fields=("id",),
        secret_fields=("credential_ref",),
    )
    target_snapshot = build_table_snapshot(
        "database_sources",
        target_rows,
        primary_key_fields=("id",),
        secret_fields=("credential_ref",),
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
                _secrets_are_redacted(row["config_json"]) and "password" not in row
                for row in canonical_rows
            ),
            "file_reachability": True,
            "lease_state": True,
            "foreign_keys": True,
        },
        check_scopes={
            "secret_redaction": "legacy password is never read into target rows; metadata is recursively redacted and credential_ref is separately redacted in snapshots",
            "file_reachability": "not_applicable: database connection metadata has no file reference",
            "lease_state": "not_applicable: database-source rows do not own a migration lease",
            "foreign_keys": "legacy knowledge_base_id ownership checked and target knowledge_spaces parent rows must pre-exist; target space_id remains an external stable ID without cross-owner FK",
        },
    )
    report.verify_safe()
    mappings = tuple(
        (
            "knowledge_database_source",
            str(row["source_key"]),
            f"knowledge://spaces/{row['space_id']}/database-sources/{row['id']}",
        )
        for row in canonical_rows
    )
    return report, mappings


@dataclass(frozen=True, slots=True)
class DatabaseSourceCatalogRehearsalResult:
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


def run_database_source_catalog_rehearsal_with_rollback_probes(
    source: Connection,
    target_engine: Engine,
    *,
    installation_id: str,
    source_revision: str,
    target_revision: str,
    active_revision: str,
    failure_checkpoints: Sequence[str] = DEFAULT_DATABASE_SOURCE_FAILURE_PROBES,
) -> DatabaseSourceCatalogRehearsalResult:
    for value, label in (
        (installation_id, "installation id"),
        (source_revision, "source revision"),
        (target_revision, "target revision"),
        (active_revision, "active revision"),
    ):
        _validate_identifier(value, label=label)
    checkpoints = tuple(failure_checkpoints)
    unknown = sorted(set(checkpoints) - DATABASE_SOURCE_FAILURE_CHECKPOINTS)
    if not checkpoints:
        raise ValueError("failure_checkpoints must not be empty")
    if unknown:
        raise ValueError(f"unsupported failure_checkpoints: {unknown}")
    if not _engines_are_independent(source.engine, target_engine):
        raise RehearsalVerificationError("source and target must use independent engines")
    if "knowledge_database_sources" not in set(inspect(source).get_table_names()):
        raise RehearsalVerificationError("source Catalog is missing knowledge_database_sources")
    with target_engine.begin() as target:
        migrate_to_latest(target)
    baseline = _target_database_state(target_engine)
    for checkpoint in checkpoints:
        try:
            with target_engine.begin() as target:
                _copy_database_source_slice(
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
        report, mappings = _copy_database_source_slice(
            source,
            target,
            source_revision=source_revision,
            target_revision=target_revision,
            active_revision=active_revision,
        )
    return DatabaseSourceCatalogRehearsalResult(
        installation_id=installation_id,
        source_tables=DATABASE_SOURCE_SOURCE_TABLES,
        target_tables=DATABASE_SOURCE_TARGET_TABLES,
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
            "knowledge_table_assets",
            "analytics_query_results",
            "knowledge_import_jobs",
            "knowledge_import_events",
            "semantic_dimension_build_jobs",
            "semantic_dimension_build_events",
        ),
        source_to_target=mappings,
        report=replace(report, injected_failure_checkpoints=checkpoints),
    )
