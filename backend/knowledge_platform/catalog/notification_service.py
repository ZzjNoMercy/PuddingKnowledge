"""Admin read boundary for Space-scoped Platform notification events."""

from __future__ import annotations

import re
from typing import Any

from knowledge_contracts import Correlation, Principal, Provenance, QueryError, QueryErrorCode, QueryResult

from .notification_scope import SqliteNotificationEventScopeStore

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")


class NotificationEventQueryService:
    """Read only events with an explicit Platform event-to-Space binding."""

    def __init__(self, store: SqliteNotificationEventScopeStore) -> None:
        self._store = store

    @staticmethod
    def _authorized(principal: Principal, space_id: str) -> bool:
        scopes = set(principal.scopes)
        return (
            principal.tenant_id is None
            and bool({"knowledge.admin", "knowledge:admin"} & scopes)
            and bool({f"knowledge.space:{space_id}", f"knowledge:space:{space_id}"} & scopes)
        )

    @staticmethod
    def _error(correlation: Correlation, code: QueryErrorCode, message: str) -> QueryResult:
        return QueryResult(status="error", trace_id=correlation.trace_id, error=QueryError(code=code, message=message))

    def list_events(
        self,
        *,
        principal: Principal,
        correlation: Correlation,
        space_id: Any,
        limit: Any = 20,
    ) -> QueryResult:
        if not isinstance(space_id, str) or _ID_RE.fullmatch(space_id) is None:
            return self._error(correlation, QueryErrorCode.INVALID_REQUEST, "space_id is invalid")
        if type(limit) is not int or not 1 <= limit <= 100:
            return self._error(correlation, QueryErrorCode.INVALID_REQUEST, "notification limit is invalid")
        if not self._authorized(principal, space_id):
            return self._error(correlation, QueryErrorCode.PERMISSION_DENIED, "Notification discovery requires Admin scope")
        try:
            revision_before = self._store.catalog_revision
            events = self._store.list_events(space_id=space_id, limit=limit)
            revision_after = self._store.catalog_revision
        except (OSError, ValueError, LookupError):
            return self._error(correlation, QueryErrorCode.CAPABILITY_UNAVAILABLE, "Platform notifications are unavailable")
        except Exception:
            return self._error(correlation, QueryErrorCode.INTERNAL_ERROR, "Platform notification read failed")
        if revision_before != revision_after:
            return self._error(correlation, QueryErrorCode.INTERNAL_ERROR, "Notification Catalog changed during read")
        return QueryResult(
            status="ok",
            trace_id=correlation.trace_id,
            data={"notifications": events, "count": len(events), "space_id": space_id},
            provenance=Provenance(
                space_id=space_id,
                dataset_id=None,
                dataset_version=None,
                capability="knowledge_list",
                catalog_revision=revision_after,
            ),
        )


__all__ = ["NotificationEventQueryService"]
