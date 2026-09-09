"""Path-free Connector OAuth status and host-managed authorization intents."""

from __future__ import annotations

import fcntl
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from knowledge_contracts import Correlation, Evidence, Principal, Provenance, QueryError, QueryErrorCode, QueryResult

from .query import CatalogQueryRepository

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")
_MODES = frozenset({"user_reauthorize", "full_replace"})
_STATUS_FIELDS = frozenset(
    {
        "connector_id",
        "space_id",
        "connector_key",
        "name",
        "connector_status",
        "auth_type",
        "grant_status",
        "active_grant_count",
        "oauth_session_status",
        "authorization_required",
        "grant_updated_at",
        "oauth_session_expires_at",
    }
)


@dataclass(frozen=True, slots=True)
class ConnectorAuthorizationRequest:
    space_id: str
    connector_id: str
    mode: str
    idempotency_key: str


def _error(correlation: Correlation, code: QueryErrorCode, message: str) -> QueryResult:
    return QueryResult(status="error", trace_id=correlation.trace_id, error=QueryError(code=code, message=message))


def _authorized(principal: Principal, space_id: str) -> bool:
    scopes = set(principal.scopes)
    return (
        principal.tenant_id is None
        and bool({"knowledge.admin", "knowledge:admin"} & scopes)
        and bool({f"knowledge.space:{space_id}", f"knowledge:space:{space_id}"} & scopes)
    )


def _valid_id(value: Any) -> bool:
    return isinstance(value, str) and _ID_RE.fullmatch(value) is not None


class ConnectorAuthorizationService:
    """Expose grant state and create intents; a host/Vault performs OAuth itself."""

    def __init__(self, repository: CatalogQueryRepository, *, staging_root: Path) -> None:
        self._repository = repository
        self._root = staging_root

    def list_status(self, *, principal: Principal, correlation: Correlation, space_id: str) -> QueryResult:
        if not isinstance(space_id, str) or not _ID_RE.fullmatch(space_id):
            return _error(correlation, QueryErrorCode.INVALID_REQUEST, "space_id is invalid")
        if not _authorized(principal, space_id):
            return _error(correlation, QueryErrorCode.PERMISSION_DENIED, "Connector authorization requires Admin scope")
        try:
            before = self._repository.catalog_revision
            items = [
                {str(key): value for key, value in item.items() if str(key) in _STATUS_FIELDS}
                for item in self._repository.list_connector_authorizations(space_id=space_id)
            ]
            after = self._repository.catalog_revision
        except Exception:
            return _error(correlation, QueryErrorCode.CAPABILITY_UNAVAILABLE, "Connector authorization status is unavailable")
        if before != after:
            return _error(correlation, QueryErrorCode.INTERNAL_ERROR, "Connector Catalog changed during authorization read")
        return QueryResult(
            status="ok",
            trace_id=correlation.trace_id,
            data={"authorizations": items, "count": len(items)},
            provenance=Provenance(space_id=space_id, dataset_id=None, dataset_version=None, capability="knowledge_list", catalog_revision=after),
        )

    def authorize(self, *, principal: Principal, correlation: Correlation, request: ConnectorAuthorizationRequest) -> QueryResult:
        if not all(_valid_id(value) for value in (request.space_id, request.connector_id, request.idempotency_key)) or request.mode not in _MODES:
            return _error(correlation, QueryErrorCode.INVALID_REQUEST, "Connector authorization request is invalid")
        if not _authorized(principal, request.space_id):
            return _error(correlation, QueryErrorCode.PERMISSION_DENIED, "Connector authorization requires Admin scope")
        try:
            before = self._repository.catalog_revision
            connector = next(
                (item for item in self._repository.list_connectors(space_id=request.space_id) if item.get("id") == request.connector_id),
                None,
            )
            if connector is None:
                return _error(correlation, QueryErrorCode.NOT_FOUND, "Connector is not available in the requested Space")
            root = self._root.expanduser().absolute()
            if root.exists() and root.is_symlink():
                raise OSError("authorization staging root must not be a symlink")
            root.mkdir(parents=True, exist_ok=True)
            record_path = root / "authorization-intents.jsonl"
            lock_path = root / ".authorization-intents.lock"
            if lock_path.is_symlink() or (record_path.exists() and record_path.is_symlink()):
                raise OSError("authorization staging files must not be symlinks")
            with lock_path.open("a+", encoding="utf-8") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                try:
                    records = [json.loads(line) for line in record_path.read_text(encoding="utf-8").splitlines()] if record_path.exists() else []
                    for record in records:
                        if record.get("idempotency_key") != request.idempotency_key:
                            continue
                        if record.get("connector_id") != request.connector_id or record.get("space_id") != request.space_id:
                            return _error(correlation, QueryErrorCode.INVALID_REQUEST, "authorization idempotency key was reused")
                        return self._intent_result(correlation, record)
                    after = self._repository.catalog_revision
                    if before != after:
                        return _error(correlation, QueryErrorCode.INTERNAL_ERROR, "Connector Catalog changed during authorization start")
                    flow_id = "auth_flow_" + hashlib.sha256(request.idempotency_key.encode()).hexdigest()[:24]
                    record = {
                        "flow_id": flow_id,
                        "space_id": request.space_id,
                        "connector_id": request.connector_id,
                        "connector_key": str(connector.get("connector_key") or ""),
                        "mode": request.mode,
                        "status": "awaiting_host_authorization",
                        "requested_at": datetime.now(UTC).isoformat(),
                        "expires_at": (datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
                        "idempotency_key": request.idempotency_key,
                    }
                    with record_path.open("a", encoding="utf-8") as stream:
                        stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
                        stream.flush()
                    return self._intent_result(correlation, record)
                finally:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        except (OSError, ValueError, json.JSONDecodeError):
            return _error(correlation, QueryErrorCode.CAPABILITY_UNAVAILABLE, "Host-managed authorization is unavailable")

    @staticmethod
    def _intent_result(correlation: Correlation, record: Mapping[str, Any]) -> QueryResult:
        data = {
            key: record[key]
            for key in ("flow_id", "space_id", "connector_id", "connector_key", "mode", "status", "requested_at", "expires_at")
        }
        return QueryResult(
            status="ok",
            trace_id=correlation.trace_id,
            answer="授权意图已创建，等待宿主/Vault 执行授权；当前未获得授权，也未返回 token 或外部 URL。",
            data={"authorization": data},
            evidence=(
                Evidence(
                    asset_id=str(record["connector_id"]),
                    resource_uri=f"knowledge://spaces/{record['space_id']}/connectors/{record['connector_id']}/authorization",
                    locator={"section": "host_managed_authorization"},
                    matched_by=("host_managed", "vault_boundary"),
                ),
            ),
            provenance=Provenance(space_id=str(record["space_id"]), dataset_id=None, dataset_version=None, capability="knowledge_list"),
        )


__all__ = ["ConnectorAuthorizationRequest", "ConnectorAuthorizationService"]
