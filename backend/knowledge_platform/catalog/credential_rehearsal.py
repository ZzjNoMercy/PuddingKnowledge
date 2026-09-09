"""Executable Phase 0B rehearsal for Feishu credential metadata and Vault refs."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import Connection
from sqlalchemy.engine import Engine

from .connector_rehearsal import (
    _CREDENTIAL_REF,
    _credential_reference,
    _looks_like_path_or_url,
    _sanitize_path_value,
)
from .migrations import migrate_to_latest
from .models import KnowledgeCredential, KnowledgeCredentialGrant, KnowledgeOAuthSession
from .rehearsal import RehearsalReport, RehearsalVerificationError, build_table_snapshot
from .rehearsal_runner import (
    RehearsalInjectedFailure,
    _json_safe,
    _out_of_scope_snapshot,
    _reflect_rows,
    _secrets_are_redacted,
    _source_ref_digest,
    _table_snapshot_rows,
    _target_database_state,
    _upsert_immutable,
)

CREDENTIAL_SOURCE_TABLES = (
    "feishu_app_credentials",
    "feishu_user_grants",
    "feishu_oauth_sessions",
)
CREDENTIAL_TARGET_TABLES = (
    "knowledge_credentials",
    "knowledge_credential_grants",
    "knowledge_oauth_sessions",
)
CREDENTIAL_FAILURE_CHECKPOINTS = frozenset(
    {"after_schema", "after_credentials", "after_grants", "after_oauth_sessions", "before_verification"}
)
DEFAULT_CREDENTIAL_FAILURE_PROBES = (
    "after_schema",
    "after_credentials",
    "after_grants",
    "after_oauth_sessions",
    "before_verification",
)
_SAFE_FEISHU_BASES = {"https://open.feishu.cn", "https://open.larksuite.com"}


def _credential_id(source_id: Any) -> str:
    return f"credential_{source_id}"


def _grant_id(source_id: Any) -> str:
    return f"grant_{source_id}"


def _oauth_session_id(source_id: Any) -> str:
    return f"oauth_{source_id}"


def _connector_id(source_id: Any) -> str:
    return f"connector_{source_id}"


def _safe_api_base(value: Any) -> str:
    normalized = str(value or "").strip().rstrip("/")
    return normalized if normalized in _SAFE_FEISHU_BASES else ""


def _safe_scalar(value: Any) -> str:
    text = str(value or "")
    if _looks_like_path_or_url(text):
        return _source_ref_digest(text)
    return text


def _safe_ref(value: Any, *, expected_name: str | None = None) -> str:
    text = str(value or "")
    normalized = _credential_reference(text)
    if not text or normalized != text:
        raise RehearsalVerificationError("credential reference is invalid or unsafe; refusing to copy it")
    if expected_name is not None and text.rsplit("/", 1)[-1] != expected_name:
        raise RehearsalVerificationError("credential reference identity does not match its owner row")
    return normalized


def _safe_state_hash(value: Any) -> str:
    text = str(value or "")
    if not text:
        raise RehearsalVerificationError("OAuth state_hash must not be empty")
    return _source_ref_digest(text)


def _oauth_is_expired(row: Mapping[str, Any], *, now: datetime | None = None) -> bool:
    """Recognize an abandoned pending OAuth handshake during local migration."""

    if str(row.get("status") or "pending") != "pending":
        return False
    value = row.get("expires_at")
    if value is None:
        return False
    if isinstance(value, datetime):
        expires_at = value
    else:
        try:
            expires_at = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    reference = now or datetime.now(timezone.utc)
    return expires_at <= reference


def _canonical_credential(row: Mapping[str, Any]) -> dict[str, Any]:
    # ``app_id_masked`` is a display value and is not guaranteed unique (the
    # local database can contain two rotations with the same mask).  Keep the
    # stable legacy row identity in the non-secret external key so the target
    # Catalog's uniqueness invariant cannot merge or drop credentials.
    external_key = f"{str(row.get('app_id_masked') or 'feishu-app')}:{row['id']}"
    return {
        "id": _credential_id(row["id"]),
        "owner_principal_id": _safe_scalar(row.get("owner_id") or "local"),
        "provider": "feishu",
        "kind": "app",
        "external_key": _safe_scalar(external_key),
        "display_name": _safe_scalar(row.get("app_name")),
        "api_base_url": _safe_api_base(row.get("api_base_url")),
        "credential_ref": _safe_ref(row.get("credential_ref"), expected_name=f"feishu-app-{row['id']}"),
        "status": str(row.get("status") or "pending_validation"),
        "metadata_json": _json_safe(
            _sanitize_path_value(
                {
                    "tenant_key": str(row.get("tenant_key") or ""),
                    "legacy_credential_id": str(row["id"]),
                }
            )
        ),
        "validated_at": row.get("validated_at"),
        "rotated_at": row.get("rotated_at"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def _canonical_grant(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": _grant_id(row["id"]),
        "credential_id": _credential_id(row["app_credential_id"]),
        "connector_id": _connector_id(row["source_connection_id"]) if row.get("source_connection_id") else None,
        "principal_id": _safe_scalar(row.get("principal_id") or "local"),
        "open_id": _safe_scalar(row.get("open_id")),
        "union_id": _safe_scalar(row.get("union_id")),
        "tenant_key": _safe_scalar(row.get("tenant_key")),
        "token_credential_ref": _safe_ref(
            row.get("token_credential_ref"),
            expected_name=f"feishu-user-grant-{row['id']}-v{int(row.get('token_version') or 1)}",
        ),
        "granted_scopes": _json_safe(_sanitize_path_value(row.get("granted_scopes") or [])),
        "access_expires_at": row.get("access_expires_at"),
        "refresh_expires_at": row.get("refresh_expires_at"),
        "token_version": int(row.get("token_version") or 1),
        "status": str(row.get("status") or "active"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def _canonical_oauth_session(row: Mapping[str, Any]) -> dict[str, Any]:
    redirect_uri = str(row.get("redirect_uri") or "")
    return {
        "id": _oauth_session_id(row["id"]),
        "state_hash": _safe_state_hash(row["state_hash"]),
        "credential_id": _credential_id(row["app_credential_id"]),
        "connector_id": _connector_id(row["source_connection_id"]),
        "principal_id": _safe_scalar(row.get("principal_id") or "local"),
        "redirect_uri_digest": _source_ref_digest(redirect_uri),
        "verifier_credential_ref": _safe_ref(
            row.get("verifier_credential_ref"), expected_name=f"feishu-oauth-verifier-{row['id']}"
        ),
        "requested_scopes": _json_safe(_sanitize_path_value(row.get("requested_scopes") or [])),
        # A local copy may contain a pending browser flow that expired before
        # this snapshot.  It is not an active grant and must not keep an
        # unbound tenant requirement alive in the target Catalog.
        "status": "expired" if _oauth_is_expired(row) else str(row.get("status") or "pending"),
        "expires_at": row["expires_at"],
        "consumed_at": row.get("consumed_at"),
        "created_at": row.get("created_at"),
    }


def _read_credential_rows(
    source: Connection,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    credentials = _reflect_rows(
        source,
        "feishu_app_credentials",
        (
            "id",
            "owner_id",
            "app_id_masked",
            "credential_ref",
            "api_base_url",
            "app_name",
            "tenant_key",
            "status",
            "validated_at",
            "rotated_at",
            "created_at",
            "updated_at",
        ),
    )
    grants = _reflect_rows(
        source,
        "feishu_user_grants",
        (
            "id",
            "app_credential_id",
            "source_connection_id",
            "principal_id",
            "open_id",
            "union_id",
            "tenant_key",
            "token_credential_ref",
            "granted_scopes",
            "access_expires_at",
            "refresh_expires_at",
            "token_version",
            "status",
            "created_at",
            "updated_at",
        ),
    )
    oauth_sessions = _reflect_rows(
        source,
        "feishu_oauth_sessions",
        (
            "id",
            "state_hash",
            "app_credential_id",
            "source_connection_id",
            "principal_id",
            "redirect_uri",
            "verifier_credential_ref",
            "requested_scopes",
            "status",
            "expires_at",
            "consumed_at",
            "created_at",
        ),
    )
    return credentials, grants, oauth_sessions


def _validate_source_relationships(
    credentials: Sequence[Mapping[str, Any]],
    grants: Sequence[Mapping[str, Any]],
    oauth_sessions: Sequence[Mapping[str, Any]],
    source_connections: Mapping[str, Mapping[str, Any]],
) -> None:
    credential_ids = {str(row["id"]) for row in credentials}
    credential_by_id = {str(row["id"]): row for row in credentials}
    grant_ids = {str(row["id"]) for row in grants}
    missing_grants = sorted(str(row["id"]) for row in grants if str(row["app_credential_id"]) not in credential_ids)
    missing_oauth_credentials = sorted(
        str(row["id"]) for row in oauth_sessions if str(row["app_credential_id"]) not in credential_ids
    )
    missing_grant_connections = sorted(
        str(row["id"])
        for row in grants
        if row.get("source_connection_id") and str(row["source_connection_id"]) not in source_connections
    )
    missing_oauth_connections = sorted(
        str(row["id"]) for row in oauth_sessions if str(row["source_connection_id"]) not in source_connections
    )
    if missing_grants or missing_oauth_credentials or missing_grant_connections or missing_oauth_connections:
        raise RehearsalVerificationError(
            "credential source relationship check failed: "
            f"grants={missing_grants}, oauth_credentials={missing_oauth_credentials}, "
            f"grant_connections={missing_grant_connections}, oauth_connections={missing_oauth_connections}"
        )
    duplicate_grants = len(grant_ids) != len(grants)
    if duplicate_grants:
        raise RehearsalVerificationError("credential grant primary keys are duplicated")
    binding_errors: list[str] = []
    for row in [*grants, *oauth_sessions]:
        app = credential_by_id.get(str(row["app_credential_id"]))
        connection = source_connections.get(str(row.get("source_connection_id") or ""))
        if app is None or connection is None:
            continue
        connection_config = connection.get("config_json") or {}
        if not isinstance(connection_config, Mapping):
            binding_errors.append(f"{row['id']}: connector config is not an object")
            continue
        bound_app_id = str(connection_config.get("app_credential_id") or "")
        if bound_app_id != str(app["id"]):
            binding_errors.append(f"{row['id']}: app={app['id']} connection_app={bound_app_id or '<missing>'}")
        app_tenant = str(app.get("tenant_key") or "")
        row_tenant = str(row.get("tenant_key") or "")
        connection_tenant = str(connection_config.get("tenant_key") or "")
        if app_tenant and connection_tenant and app_tenant != connection_tenant:
            binding_errors.append(f"{row['id']}: app_tenant differs from connection_tenant")
        if app_tenant and row_tenant and app_tenant != row_tenant:
            binding_errors.append(f"{row['id']}: app_tenant differs from grant_tenant")
        if connection_tenant and row_tenant and connection_tenant != row_tenant:
            binding_errors.append(f"{row['id']}: connection_tenant differs from grant_tenant")
        if not (app_tenant or connection_tenant or row_tenant) and not _oauth_is_expired(row):
            binding_errors.append(f"{row['id']}: tenant binding is absent")
        if "redirect_uri" not in row and (app_tenant or connection_tenant) and not row_tenant:
            binding_errors.append(f"{row['id']}: grant tenant is absent")
    if binding_errors:
        raise RehearsalVerificationError(f"credential app/connector/tenant binding check failed: {binding_errors}")


def _copy_credential_slice(
    source: Connection,
    target: Connection,
    *,
    source_revision: str,
    target_revision: str,
    active_revision: str,
    failure_checkpoint: str | None = None,
) -> tuple[RehearsalReport, tuple[tuple[str, str, str], ...]]:
    if failure_checkpoint is not None and failure_checkpoint not in CREDENTIAL_FAILURE_CHECKPOINTS:
        raise ValueError(f"unsupported credential failure checkpoint: {failure_checkpoint}")
    credentials, grants, oauth_sessions = _read_credential_rows(source)
    source_connections = {
        str(row["id"]): row for row in _reflect_rows(source, "knowledge_source_connections", ("id", "config_json"))
    }
    _validate_source_relationships(credentials, grants, oauth_sessions, source_connections)
    credential_rows = [_canonical_credential(row) for row in credentials]
    grant_rows = [_canonical_grant(row) for row in grants]
    oauth_rows = [_canonical_oauth_session(row) for row in oauth_sessions]
    migrate_to_latest(target)
    if failure_checkpoint == "after_schema":
        raise RehearsalInjectedFailure("injected credential rehearsal failure at after_schema")
    credential_table = KnowledgeCredential.__table__
    grant_table = KnowledgeCredentialGrant.__table__
    oauth_table = KnowledgeOAuthSession.__table__
    for row in credential_rows:
        _upsert_immutable(target, credential_table, row, ("id",))
    if failure_checkpoint == "after_credentials":
        raise RehearsalInjectedFailure("injected credential rehearsal failure at after_credentials")
    for row in grant_rows:
        _upsert_immutable(target, grant_table, row, ("id",))
    if failure_checkpoint == "after_grants":
        raise RehearsalInjectedFailure("injected credential rehearsal failure at after_grants")
    for row in oauth_rows:
        _upsert_immutable(target, oauth_table, row, ("id",))
    if failure_checkpoint == "after_oauth_sessions":
        raise RehearsalInjectedFailure("injected credential rehearsal failure at after_oauth_sessions")
    for row in credential_rows:
        _upsert_immutable(target, credential_table, row, ("id",))
    for row in grant_rows:
        _upsert_immutable(target, grant_table, row, ("id",))
    for row in oauth_rows:
        _upsert_immutable(target, oauth_table, row, ("id",))
    if failure_checkpoint == "before_verification":
        raise RehearsalInjectedFailure("injected credential rehearsal failure at before_verification")

    canonical = {"credentials": credential_rows, "grants": grant_rows, "oauth_sessions": oauth_rows}
    source_snapshot_rows = {name: [_json_safe(row) for row in rows] for name, rows in canonical.items()}
    target_tables = {
        "credentials": (credential_table, credential_rows),
        "grants": (grant_table, grant_rows),
        "oauth_sessions": (oauth_table, oauth_rows),
    }
    target_rows = {
        name: _table_snapshot_rows(
            target,
            table,
            fields=tuple(rows[0]) if rows else tuple(table.c.keys()),
            primary_key_fields=("id",),
            expected_primary_keys=tuple((row["id"],) for row in rows),
        )
        for name, (table, rows) in target_tables.items()
    }
    out_of_scope = tuple(
        snapshot
        for name, (table, rows) in target_tables.items()
        if (
            snapshot := _out_of_scope_snapshot(
                target,
                table,
                fields=tuple(target_rows[name][0]) if target_rows[name] else tuple(table.c.keys()),
                primary_key_fields=("id",),
                expected_primary_keys=tuple((row["id"],) for row in rows),
            )
        )
        is not None
    )
    source_snapshots = tuple(
        build_table_snapshot(name, rows, primary_key_fields=("id",)) for name, rows in source_snapshot_rows.items()
    )
    target_snapshots = tuple(
        build_table_snapshot(name, target_rows[name], primary_key_fields=("id",)) for name in canonical
    )
    refs = [row[field] for row in credential_rows for field in ("credential_ref",)]
    refs += [row["token_credential_ref"] for row in grant_rows]
    refs += [row["verifier_credential_ref"] for row in oauth_rows]
    metadata_values = [row["metadata_json"] for row in credential_rows]
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
            "secret_redaction": all(
                value in ("", "<redacted>") or _CREDENTIAL_REF.fullmatch(str(value)) is not None for value in refs
            )
            and all(_secrets_are_redacted(value) for value in metadata_values),
            "file_reachability": True,
            "lease_state": True,
            "foreign_keys": True,
        },
        check_scopes={
            "secret_redaction": "Feishu app, OAuth grant and PKCE verifier references; no secret payload is copied",
            "file_reachability": "credential metadata slice contains no file references",
            "lease_state": "credential metadata slice contains no job leases",
            "foreign_keys": "app credential ownership is checked before grant/OAuth mapping; target uses stable IDs",
        },
    )
    report.verify_safe()
    mappings = tuple(
        [
            ("feishu_app_credential", str(row["id"]), f"knowledge://credentials/{_credential_id(row['id'])}")
            for row in credentials
        ]
        + [
            ("feishu_user_grant", str(row["id"]), f"knowledge://credentials/grants/{_grant_id(row['id'])}")
            for row in grants
        ]
        + [
            ("feishu_oauth_session", str(row["id"]), f"knowledge://credentials/oauth/{_oauth_session_id(row['id'])}")
            for row in oauth_sessions
        ]
    )
    return report, mappings


@dataclass(frozen=True, slots=True)
class CredentialCatalogRehearsalResult:
    """Machine-readable evidence for the Feishu credential metadata slice."""

    installation_id: str
    source_tables: tuple[str, ...]
    target_tables: tuple[str, ...]
    source_to_target: tuple[tuple[str, str, str], ...]
    report: RehearsalReport

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": "agent-knowledge-platform-catalog-rehearsal/v1",
            "installation_id": self.installation_id,
            "source_tables": list(self.source_tables),
            "target_tables": list(self.target_tables),
            "excluded_source_tables": ["feishu_access_tokens", "feishu_refresh_tokens", "oauth_verifiers"],
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


def run_feishu_credential_catalog_rehearsal_with_rollback_probes(
    source: Connection,
    target_engine: Engine,
    *,
    installation_id: str,
    source_revision: str,
    target_revision: str,
    active_revision: str,
    failure_checkpoints: Sequence[str] = DEFAULT_CREDENTIAL_FAILURE_PROBES,
) -> CredentialCatalogRehearsalResult:
    """Probe credential metadata copy failures, then perform a successful replay."""

    checkpoints = tuple(failure_checkpoints)
    unknown = sorted(set(checkpoints) - CREDENTIAL_FAILURE_CHECKPOINTS)
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
                _copy_credential_slice(
                    source,
                    target,
                    source_revision=source_revision,
                    target_revision=f"{target_revision}-probe-{checkpoint}",
                    active_revision=active_revision,
                    failure_checkpoint=checkpoint,
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
        report, mappings = _copy_credential_slice(
            source,
            target,
            source_revision=source_revision,
            target_revision=target_revision,
            active_revision=active_revision,
        )
    return CredentialCatalogRehearsalResult(
        installation_id=installation_id,
        source_tables=CREDENTIAL_SOURCE_TABLES,
        target_tables=CREDENTIAL_TARGET_TABLES,
        source_to_target=mappings,
        report=replace(report, injected_failure_checkpoints=checkpoints),
    )
