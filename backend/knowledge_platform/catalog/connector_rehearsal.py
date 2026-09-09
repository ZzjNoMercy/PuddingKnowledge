"""Executable Phase 0B rehearsal for connector identity and sync leases."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from sqlalchemy import Connection
from sqlalchemy.engine import Engine

from .migrations import migrate_to_latest
from .models import KnowledgeConnector, KnowledgeSourceItem, KnowledgeSyncRun
from .rehearsal import RehearsalReport, RehearsalVerificationError, build_table_snapshot
from .rehearsal_runner import (
    _SECRET_KEY,
    RehearsalInjectedFailure,
    _json_safe,
    _out_of_scope_snapshot,
    _redact,
    _reflect_rows,
    _secrets_are_redacted,
    _source_ref_digest,
    _table_snapshot_rows,
    _target_database_state,
    _upsert_immutable,
    run_core_catalog_rehearsal,
)

CONNECTOR_SOURCE_TABLES = ("knowledge_source_connections", "knowledge_source_items", "knowledge_sync_runs")
CONNECTOR_TARGET_TABLES = ("knowledge_connectors", "knowledge_source_items", "knowledge_sync_runs")
CONNECTOR_FAILURE_CHECKPOINTS = frozenset(
    {
        "after_schema",
        "after_spaces",
        "after_assets",
        "after_datasets",
        "after_connectors",
        "after_source_items",
        "after_sync_runs",
        "before_verification",
    }
)
DEFAULT_CONNECTOR_FAILURE_PROBES = (
    "after_schema",
    "after_spaces",
    "after_assets",
    "after_datasets",
    "after_connectors",
    "after_source_items",
    "after_sync_runs",
    "before_verification",
)
_CREDENTIAL_REF = re.compile(r"^(?:secret|vault|credential|ref)://[A-Za-z0-9._-]+/[A-Za-z0-9._/-]+$", re.IGNORECASE)
_SUSPICIOUS_REF_COMPONENT = re.compile(
    r"(?:raw|plain(?:text)?|secret|token|password|bearer|private[_-]?key)", re.IGNORECASE
)
_WINDOWS_ABSOLUTE_PATH = re.compile(r"^[A-Za-z]:[\\/]")


def _is_nonportable_path(value: str) -> bool:
    return value.startswith(("/", "\\\\", "//", "./", "../")) or bool(_WINDOWS_ABSOLUTE_PATH.match(value))


def _is_physical_path(value: str) -> bool:
    if value.startswith("file://"):
        parsed = urlparse(value)
        return parsed.netloc in {"", "localhost"} and bool(parsed.path)
    return _is_nonportable_path(value)


def _looks_like_path_or_url(value: str) -> bool:
    return _is_nonportable_path(value) or "://" in value or ("/" in value and "." in value)


def _connector_id(source_id: Any) -> str:
    return f"connector_{source_id}"


def _space_id(source_id: Any) -> str:
    return f"space_{source_id}"


def _sync_run_id(source_id: Any) -> str:
    return f"sync_{source_id}"


def _source_item_id(source_id: Any) -> str:
    return f"source_item_{source_id}"


def _asset_id(document_id: Any, source_revision: str) -> str:
    revision_digest = hashlib.sha256(source_revision.encode("utf-8")).hexdigest()[:12]
    return f"asset_{document_id}_{revision_digest}"


def _credential_reference(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    # Older local Catalogs persisted the opaque Feishu app identity outside
    # the newer URI-shaped credential-ref contract.  Preserve only that
    # stable identity under a local reference; never treat the legacy value as
    # a secret or copy it into a provider credential field.
    legacy_feishu = re.fullmatch(r"feishu-app:([A-Za-z0-9._-]+)", text)
    if legacy_feishu:
        return f"ref://local/feishu-app/{legacy_feishu.group(1)}"
    if not _CREDENTIAL_REF.fullmatch(text):
        return "<redacted>"
    if any(marker in text for marker in ("?", "#", "@")):
        return "<redacted>"
    components = text.split("://", 1)[1].split("/")
    if len(components) >= 2 and components[0].lower() == "users" and components[1].lower() == "public":
        return "<redacted>"
    if _SUSPICIOUS_REF_COMPONENT.search(components[-1]):
        return "<redacted>"
    return text


def _sanitize_path_value(value: Any) -> Any:
    if isinstance(value, str):
        if _looks_like_path_or_url(value):
            return {"reference_digest": _source_ref_digest(value)}
        return _redact(value)
    if isinstance(value, Mapping):
        return {
            str(key): "<redacted>" if _SECRET_KEY.search(str(key)) else _sanitize_path_value(child)
            for key, child in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_sanitize_path_value(child) for child in value]
    return value


def _physical_path_references(value: Any) -> list[str]:
    if isinstance(value, str):
        if value.startswith("file://"):
            parsed = urlparse(value)
            if parsed.netloc not in {"", "localhost"}:
                return []
            return [unquote(parsed.path)] if parsed.path else []
        return [value] if _is_physical_path(value) else []
    if isinstance(value, Mapping):
        return [reference for child in value.values() for reference in _physical_path_references(child)]
    if isinstance(value, (list, tuple)):
        return [reference for child in value for reference in _physical_path_references(child)]
    return []


def _as_utc(value: Any) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _lease_state_is_valid(sync_runs: Sequence[Mapping[str, Any]], *, as_of: datetime | None) -> bool:
    as_of_utc = _as_utc(as_of)
    allowed_statuses = {"queued", "running", "succeeded", "succeeded_with_errors", "failed", "cancelled"}
    terminal_statuses = {"succeeded", "succeeded_with_errors", "failed", "cancelled"}
    for sync_run in sync_runs:
        status = str(sync_run["status"])
        if status not in allowed_statuses:
            return False
        if int(sync_run["progress"]) < 0 or int(sync_run["progress"]) > 100:
            return False
        if int(sync_run["attempt"]) < 0:
            return False
        owner = str(sync_run["lease_owner"] or "")
        expires_at = _as_utc(sync_run["lease_expires_at"])
        heartbeat_at = _as_utc(sync_run["heartbeat_at"])
        if status == "running":
            if not owner or expires_at is None or heartbeat_at is None:
                return False
            if heartbeat_at > expires_at:
                return False
            if as_of_utc is not None and expires_at < as_of_utc:
                return False
            if as_of_utc is not None and heartbeat_at > as_of_utc:
                return False
        elif status == "queued" and (owner or expires_at is not None or heartbeat_at is not None):
            return False
        elif status in terminal_statuses and (owner or expires_at is not None or heartbeat_at is not None):
            return False
        elif owner and expires_at is None:
            return False
    if any(str(sync_run["status"]) == "running" for sync_run in sync_runs) and as_of_utc is None:
        return False
    return True


def _canonical_connector(row: Mapping[str, Any]) -> dict[str, Any]:
    last_sync_run_id = row.get("last_sync_run_id")
    return {
        "id": _connector_id(row["id"]),
        "space_id": _space_id(row["knowledge_base_id"]),
        "connector_key": str(row["connector_key"]),
        "name": str(row["name"]),
        "status": str(row.get("status") or "ready"),
        "auth_type": str(row.get("auth_type") or "builtin"),
        "credential_ref": _credential_reference(row.get("credential_ref")),
        "config_json": _json_safe(_sanitize_path_value(row.get("config_json") or {})),
        "schedule_json": _json_safe(_sanitize_path_value(row.get("schedule_json") or {})),
        "last_sync_run_id": _sync_run_id(last_sync_run_id) if last_sync_run_id else None,
        "last_synced_at": row.get("last_synced_at"),
        "last_error_json": _json_safe(_sanitize_path_value(row.get("last_error_json") or {})),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def _canonical_source_item(row: Mapping[str, Any], *, source_revision: str) -> dict[str, Any]:
    document_id = row.get("document_id")
    asset_id = _asset_id(document_id, source_revision) if document_id else None
    content_sha = str(row.get("content_sha256") or "")
    content_digest = content_sha if not content_sha or content_sha.startswith("sha256:") else f"sha256:{content_sha}"
    raw_source_url = str(row.get("source_url") or "")
    metadata = _json_safe(_sanitize_path_value(row.get("metadata_json") or {}))
    if raw_source_url:
        metadata["source_url_digest"] = _source_ref_digest(raw_source_url)
    return {
        "id": _source_item_id(row["id"]),
        "space_id": _space_id(row["knowledge_base_id"]),
        "connector_id": _connector_id(row["source_connection_id"]),
        "external_id": str(row["external_id"]),
        "external_parent_id": row.get("external_parent_id"),
        "external_type": str(row["external_type"]),
        "title": str(row.get("title") or ""),
        # URLs can carry credentials in path, host, query or userinfo. Keep
        # only a digest in metadata and force the target URL field to null.
        "source_url": None,
        "path_json": _json_safe(_sanitize_path_value(row.get("path_json") or [])),
        "revision": str(row["revision"]) if row.get("revision") is not None else None,
        "content_digest": content_digest or None,
        "asset_id": asset_id,
        "status": str(row.get("status") or "discovered"),
        "remote_created_at": row.get("remote_created_at"),
        "remote_updated_at": row.get("remote_updated_at"),
        "last_seen_sync_run_id": _sync_run_id(row["last_seen_run_id"]) if row.get("last_seen_run_id") else None,
        "metadata_json": metadata,
        "permissions_json": _json_safe(_sanitize_path_value(row.get("permissions_json") or {})),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def _canonical_sync_run(row: Mapping[str, Any]) -> dict[str, Any]:
    status = str(row.get("status") or "queued")
    # A cancelled/terminal legacy run may retain the worker's old lease
    # bookkeeping.  The target contract treats terminal work as released;
    # carrying that stale owner forward would make a safe local snapshot look
    # actively leased and would prevent a later worker from reclaiming it.
    lease_owner = row.get("lease_owner")
    lease_expires_at = row.get("lease_expires_at")
    heartbeat_at = row.get("heartbeat_at")
    if status in {"succeeded", "succeeded_with_errors", "failed", "cancelled"}:
        lease_owner = None
        lease_expires_at = None
        heartbeat_at = None
    return {
        "id": _sync_run_id(row["id"]),
        "connector_id": _connector_id(row["source_connection_id"]),
        "mode": str(row.get("mode") or "incremental"),
        "status": status,
        "cursor_json": _json_safe(_sanitize_path_value(row.get("cursor_json") or {})),
        "stats_json": _json_safe(_sanitize_path_value(row.get("stats_json") or {})),
        "current_step": str(row.get("current_step") or "queued"),
        "progress": int(row.get("progress") or 0),
        "error_json": _json_safe(_sanitize_path_value(row.get("error_json") or {})),
        "started_at": row.get("started_at"),
        "finished_at": row.get("finished_at"),
        "lease_owner": lease_owner,
        "lease_expires_at": lease_expires_at,
        "heartbeat_at": heartbeat_at,
        "attempt": int(row.get("attempt") or 0),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def _copy_connector_slice(
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
    if failure_checkpoint is not None and failure_checkpoint not in CONNECTOR_FAILURE_CHECKPOINTS:
        raise ValueError(f"unsupported connector failure checkpoint: {failure_checkpoint}")
    connection_rows = _reflect_rows(
        source,
        "knowledge_source_connections",
        (
            "id",
            "knowledge_base_id",
            "connector_key",
            "name",
            "status",
            "auth_type",
            "credential_ref",
            "config_json",
            "schedule_json",
            "last_sync_run_id",
            "last_synced_at",
            "last_error_json",
            "created_at",
            "updated_at",
        ),
    )
    base_rows = _reflect_rows(source, "knowledge_bases", ("id",))
    item_rows = _reflect_rows(
        source,
        "knowledge_source_items",
        (
            "id",
            "knowledge_base_id",
            "source_connection_id",
            "external_id",
            "external_parent_id",
            "external_type",
            "title",
            "source_url",
            "path_json",
            "revision",
            "content_sha256",
            "document_id",
            "status",
            "remote_created_at",
            "remote_updated_at",
            "last_seen_run_id",
            "metadata_json",
            "permissions_json",
            "created_at",
            "updated_at",
        ),
    )
    sync_rows = _reflect_rows(
        source,
        "knowledge_sync_runs",
        (
            "id",
            "source_connection_id",
            "mode",
            "status",
            "cursor_json",
            "stats_json",
            "current_step",
            "progress",
            "error_json",
            "started_at",
            "finished_at",
            "lease_owner",
            "lease_expires_at",
            "heartbeat_at",
            "attempt",
            "created_at",
            "updated_at",
        ),
    )
    connection_ids = {str(row["id"]) for row in connection_rows}
    base_ids = {str(row["id"]) for row in base_rows}
    connection_base_orphans = sorted(
        str(row["id"]) for row in connection_rows if str(row["knowledge_base_id"]) not in base_ids
    )
    connection_to_base = {str(row["id"]): str(row["knowledge_base_id"]) for row in connection_rows}
    item_base_mismatches = sorted(
        str(row["id"])
        for row in item_rows
        if connection_to_base.get(str(row["source_connection_id"])) != str(row["knowledge_base_id"])
    )
    item_orphans = sorted(str(row["id"]) for row in item_rows if str(row["source_connection_id"]) not in connection_ids)
    sync_orphans = sorted(str(row["id"]) for row in sync_rows if str(row["source_connection_id"]) not in connection_ids)
    sync_ids = {str(row["id"]) for row in sync_rows}
    sync_to_connection = {str(row["id"]): str(row["source_connection_id"]) for row in sync_rows}
    connector_last_sync_orphans = sorted(
        str(row["id"])
        for row in connection_rows
        if row.get("last_sync_run_id") and str(row["last_sync_run_id"]) not in sync_ids
    )
    item_last_seen_orphans = sorted(
        str(row["id"])
        for row in item_rows
        if row.get("last_seen_run_id") and str(row["last_seen_run_id"]) not in sync_ids
    )
    connector_last_sync_mismatches = sorted(
        str(row["id"])
        for row in connection_rows
        if row.get("last_sync_run_id") and sync_to_connection.get(str(row["last_sync_run_id"])) != str(row["id"])
    )
    item_last_seen_mismatches = sorted(
        str(row["id"])
        for row in item_rows
        if row.get("last_seen_run_id")
        and sync_to_connection.get(str(row["last_seen_run_id"])) != str(row["source_connection_id"])
    )
    if (
        connection_base_orphans
        or item_orphans
        or item_base_mismatches
        or sync_orphans
        or connector_last_sync_orphans
        or connector_last_sync_mismatches
        or item_last_seen_orphans
        or item_last_seen_mismatches
    ):
        raise RehearsalVerificationError(
            "source connector foreign-key check failed: "
            f"connection_bases={connection_base_orphans}, items={item_orphans}, "
            f"item_bases={item_base_mismatches}, sync_runs={sync_orphans}, "
            f"connector_last_sync={connector_last_sync_orphans + connector_last_sync_mismatches}, "
            f"item_last_seen={item_last_seen_orphans + item_last_seen_mismatches}"
        )

    physical_references = sorted(
        {reference for row in item_rows for reference in _physical_path_references(row.get("path_json") or [])}
    )
    if physical_references and file_reference_checker is None:
        raise RehearsalVerificationError(
            "file_reference_checker is required when connector item paths contain physical references"
        )
    file_reachable = not physical_references or all(
        file_reference_checker(reference) for reference in physical_references
    )
    if not file_reachable:
        raise RehearsalVerificationError("connector source file-reference reachability check failed")

    connectors = [_canonical_connector(row) for row in connection_rows]
    source_items = [_canonical_source_item(row, source_revision=source_revision) for row in item_rows]
    sync_runs = [_canonical_sync_run(row) for row in sync_rows]
    migrate_to_latest(target)
    target_connector_table = KnowledgeConnector.__table__
    target_item_table = KnowledgeSourceItem.__table__
    target_sync_table = KnowledgeSyncRun.__table__
    for connector in connectors:
        _upsert_immutable(target, target_connector_table, connector, ("id",))
    if failure_checkpoint == "after_connectors":
        raise RehearsalInjectedFailure("injected connector rehearsal failure at after_connectors")
    for item in source_items:
        _upsert_immutable(target, target_item_table, item, ("id",))
    if failure_checkpoint == "after_source_items":
        raise RehearsalInjectedFailure("injected connector rehearsal failure at after_source_items")
    for sync_run in sync_runs:
        _upsert_immutable(target, target_sync_table, sync_run, ("id",))
    if failure_checkpoint == "after_sync_runs":
        raise RehearsalInjectedFailure("injected connector rehearsal failure at after_sync_runs")

    for connector in connectors:
        _upsert_immutable(target, target_connector_table, connector, ("id",))
    for item in source_items:
        _upsert_immutable(target, target_item_table, item, ("id",))
    for sync_run in sync_runs:
        _upsert_immutable(target, target_sync_table, sync_run, ("id",))
    if failure_checkpoint == "before_verification":
        raise RehearsalInjectedFailure("injected connector rehearsal failure at before_verification")

    source_snapshot_rows = {
        "connectors": [_json_safe(row) for row in connectors],
        "source_items": [_json_safe(row) for row in source_items],
        "sync_runs": [_json_safe(row) for row in sync_runs],
    }
    target_snapshot_rows = {
        "connectors": _table_snapshot_rows(
            target,
            target_connector_table,
            fields=tuple(connectors[0]) if connectors else tuple(target_connector_table.c.keys()),
            primary_key_fields=("id",),
            expected_primary_keys=tuple((connector["id"],) for connector in connectors),
        ),
        "source_items": _table_snapshot_rows(
            target,
            target_item_table,
            fields=tuple(source_items[0]) if source_items else tuple(target_item_table.c.keys()),
            primary_key_fields=("id",),
            expected_primary_keys=tuple((item["id"],) for item in source_items),
        ),
        "sync_runs": _table_snapshot_rows(
            target,
            target_sync_table,
            fields=tuple(sync_runs[0]) if sync_runs else tuple(target_sync_table.c.keys()),
            primary_key_fields=("id",),
            expected_primary_keys=tuple((sync_run["id"],) for sync_run in sync_runs),
        ),
    }
    if not connectors:
        target_snapshot_rows["connectors"] = []
    if not source_items:
        target_snapshot_rows["source_items"] = []
    if not sync_runs:
        target_snapshot_rows["sync_runs"] = []
    lease_fields = ("status", "lease_owner", "lease_expires_at", "heartbeat_at", "attempt")
    expected_keys = {
        "connectors": tuple((connector["id"],) for connector in connectors),
        "source_items": tuple((item["id"],) for item in source_items),
        "sync_runs": tuple((sync_run["id"],) for sync_run in sync_runs),
    }
    target_out_of_scope_tables = tuple(
        snapshot
        for name, table in (
            ("connectors", target_connector_table),
            ("source_items", target_item_table),
            ("sync_runs", target_sync_table),
        )
        if (
            snapshot := _out_of_scope_snapshot(
                target,
                table,
                fields=tuple(target_snapshot_rows[name][0]) if target_snapshot_rows[name] else tuple(table.c.keys()),
                primary_key_fields=("id",),
                expected_primary_keys=expected_keys[name],
                lease_fields=lease_fields if name == "sync_runs" else (),
            )
        )
        is not None
    )
    source_snapshots = tuple(
        build_table_snapshot(
            name,
            rows,
            primary_key_fields=("id",),
            lease_fields=lease_fields if name == "sync_runs" else (),
        )
        for name, rows in source_snapshot_rows.items()
    )
    target_snapshots = tuple(
        build_table_snapshot(
            name,
            rows,
            primary_key_fields=("id",),
            lease_fields=lease_fields if name == "sync_runs" else (),
        )
        for name, rows in target_snapshot_rows.items()
    )
    credential_refs = [connector["credential_ref"] for connector in connectors]
    secret_values = (
        [connector[field] for connector in connectors for field in ("config_json", "schedule_json", "last_error_json")]
        + [item[field] for item in source_items for field in ("path_json", "metadata_json", "permissions_json")]
        + [sync_run[field] for sync_run in sync_runs for field in ("cursor_json", "stats_json", "error_json")]
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
            "secret_redaction": all(
                value in ("", "<redacted>") or _CREDENTIAL_REF.match(str(value)) is not None
                for value in credential_refs
            )
            and all(_secrets_are_redacted(value) for value in secret_values),
            "file_reachability": file_reachable,
            "lease_state": _lease_state_is_valid(sync_runs, as_of=lease_as_of),
            "foreign_keys": True,
        },
        check_scopes={
            "secret_redaction": "connector config, source-item metadata and sync cursor/error JSON",
            "file_reachability": "physical paths in source-item path_json checked by the explicit file_reference_checker",
            "lease_state": "sync run status, owner, expiry, heartbeat and attempt are copied and snapshotted; expiry checked against explicit rehearsal as_of when supplied",
            "foreign_keys": "source KB/connection/item/sync-run references checked; target uses stable application IDs",
        },
    )
    report.verify_safe()
    mappings = tuple(
        [
            (
                "source_connection",
                str(row["id"]),
                f"knowledge://spaces/{_space_id(row['knowledge_base_id'])}/connectors/{_connector_id(row['id'])}",
            )
            for row in connection_rows
        ]
        + [
            (
                "source_item",
                str(row["id"]),
                f"knowledge://spaces/{_space_id(row['knowledge_base_id'])}/source-items/{_source_item_id(row['id'])}",
            )
            for row in item_rows
        ]
        + [
            (
                "sync_run",
                str(row["id"]),
                f"knowledge://spaces/connector-runs/{_sync_run_id(row['id'])}",
            )
            for row in sync_rows
        ]
    )
    return report, mappings


@dataclass(frozen=True, slots=True)
class ConnectorCatalogRehearsalResult:
    """Machine-readable evidence for the core plus connector rehearsal slice."""

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


def _run_combined_once(
    source: Connection,
    target: Connection,
    *,
    installation_id: str,
    source_revision: str,
    target_revision: str,
    active_revision: str,
    failure_checkpoint: str | None,
    file_reference_checker: Callable[[str], bool] | None,
    lease_as_of: datetime | None,
) -> ConnectorCatalogRehearsalResult:
    core_failure = (
        failure_checkpoint
        if failure_checkpoint in {"after_schema", "after_spaces", "after_assets", "after_datasets"}
        else None
    )
    core_result = run_core_catalog_rehearsal(
        source,
        target,
        installation_id=installation_id,
        source_revision=source_revision,
        target_revision=target_revision,
        active_revision=active_revision,
        file_reference_checker=file_reference_checker,
        failure_checkpoint=core_failure,
    )
    connector_report, connector_mappings = _copy_connector_slice(
        source,
        target,
        source_revision=source_revision,
        target_revision=target_revision,
        active_revision=active_revision,
        failure_checkpoint=failure_checkpoint,
        lease_as_of=lease_as_of,
        file_reference_checker=file_reference_checker,
    )
    combined_report = replace(
        core_result.report,
        source_tables=core_result.report.source_tables + connector_report.source_tables,
        target_tables=core_result.report.target_tables + connector_report.target_tables,
        target_out_of_scope_tables=(
            core_result.report.target_out_of_scope_tables + connector_report.target_out_of_scope_tables
        ),
        checks={
            check: core_result.report.checks[check] and connector_report.checks[check]
            for check in core_result.report.checks
        },
        check_scopes={
            check: f"core: {core_result.report.check_scopes[check]}; connector: {connector_report.check_scopes[check]}"
            for check in core_result.report.checks
        },
        retry_idempotent=core_result.report.retry_idempotent and connector_report.retry_idempotent,
    )
    combined_report.verify_safe()
    result = ConnectorCatalogRehearsalResult(
        installation_id=installation_id,
        source_tables=core_result.source_tables + CONNECTOR_SOURCE_TABLES,
        target_tables=core_result.target_tables + CONNECTOR_TARGET_TABLES,
        excluded_source_tables=(
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
            "task_notifications",
        ),
        source_to_target=core_result.source_to_target + connector_mappings,
        report=combined_report,
    )
    return result


def run_connector_catalog_rehearsal_with_rollback_probes(
    source: Connection,
    target_engine: Engine,
    *,
    installation_id: str,
    source_revision: str,
    target_revision: str,
    active_revision: str,
    failure_checkpoints: Sequence[str] = DEFAULT_CONNECTOR_FAILURE_PROBES,
    file_reference_checker: Callable[[str], bool] | None = None,
    lease_as_of: datetime | None = None,
) -> ConnectorCatalogRehearsalResult:
    """Probe core+connector copy failures, then perform the successful replay."""

    checkpoints = tuple(failure_checkpoints)
    unknown = sorted(set(checkpoints) - CONNECTOR_FAILURE_CHECKPOINTS)
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
                _run_combined_once(
                    source,
                    target,
                    installation_id=installation_id,
                    source_revision=source_revision,
                    target_revision=f"{target_revision}-probe-{checkpoint}",
                    active_revision=active_revision,
                    failure_checkpoint=checkpoint,
                    file_reference_checker=file_reference_checker,
                    lease_as_of=lease_as_of,
                )
        except RehearsalInjectedFailure:
            pass
        except RehearsalVerificationError:
            raise
        else:
            raise RehearsalVerificationError(f"failure probe did not fail at {checkpoint}")
        after_failure = _target_database_state(target_engine)
        if after_failure != baseline:
            raise RehearsalVerificationError(
                f"target database state changed after rollback probe {checkpoint}: "
                f"baseline={baseline}, after={after_failure}"
            )
    with target_engine.begin() as target:
        result = _run_combined_once(
            source,
            target,
            installation_id=installation_id,
            source_revision=source_revision,
            target_revision=target_revision,
            active_revision=active_revision,
            failure_checkpoint=None,
            file_reference_checker=file_reference_checker,
            lease_as_of=lease_as_of,
        )
    return replace(result, report=replace(result.report, injected_failure_checkpoints=checkpoints))
