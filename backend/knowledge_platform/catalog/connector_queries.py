"""Read-only, path-free Connector and SourceItem discovery for Admin."""

from __future__ import annotations

import re
from typing import Any

from knowledge_contracts import Correlation, Principal, Provenance, QueryError, QueryErrorCode, QueryResult

from .query import CatalogQueryRepository

_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")


class CatalogConnectorQueryService:
    """Expose only non-secret connector identity and sync state."""

    def __init__(self, repository: CatalogQueryRepository) -> None:
        self._repository = repository

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

    def _validate(self, *, principal: Principal, correlation: Correlation, space_id: Any, connector_id: Any = None) -> QueryResult | None:
        if not isinstance(space_id, str) or not _ID_RE.fullmatch(space_id):
            return self._error(correlation, QueryErrorCode.INVALID_REQUEST, "space_id is invalid")
        if connector_id is not None and (not isinstance(connector_id, str) or not _ID_RE.fullmatch(connector_id)):
            return self._error(correlation, QueryErrorCode.INVALID_REQUEST, "connector_id is invalid")
        if not self._authorized(principal, space_id):
            return self._error(correlation, QueryErrorCode.PERMISSION_DENIED, "Connector discovery requires Admin scope")
        return None

    @staticmethod
    def _provenance(space_id: str, capability: str, revision: str | None) -> Provenance:
        return Provenance(space_id=space_id, dataset_id=None, dataset_version=None, capability=capability, catalog_revision=revision)

    def list_connectors(self, *, principal: Principal, correlation: Correlation, space_id: str) -> QueryResult:
        if error := self._validate(principal=principal, correlation=correlation, space_id=space_id):
            return error
        try:
            revision_before = self._repository.catalog_revision
            connectors = [dict(item) for item in self._repository.list_connectors(space_id=space_id)]
            revision_after = self._repository.catalog_revision
        except Exception:
            return self._error(correlation, QueryErrorCode.CAPABILITY_UNAVAILABLE, "Connector Catalog is unavailable")
        if revision_before != revision_after:
            return self._error(correlation, QueryErrorCode.INTERNAL_ERROR, "Connector Catalog changed during query")
        return QueryResult(
            status="ok",
            trace_id=correlation.trace_id,
            data={"connectors": connectors, "count": len(connectors)},
            provenance=self._provenance(space_id, "knowledge_list", revision_after),
        )

    def list_source_items(
        self, *, principal: Principal, correlation: Correlation, space_id: str, connector_id: str | None = None
    ) -> QueryResult:
        if error := self._validate(principal=principal, correlation=correlation, space_id=space_id, connector_id=connector_id):
            return error
        try:
            revision_before = self._repository.catalog_revision
            if connector_id is not None and not any(
                str(item.get("id")) == connector_id for item in self._repository.list_connectors(space_id=space_id)
            ):
                return self._error(correlation, QueryErrorCode.NOT_FOUND, "Connector is not available in the requested Space")
            items = [dict(item) for item in self._repository.list_source_items(space_id=space_id, connector_id=connector_id)]
            revision_after = self._repository.catalog_revision
        except Exception:
            return self._error(correlation, QueryErrorCode.CAPABILITY_UNAVAILABLE, "Source Item Catalog is unavailable")
        if revision_before != revision_after:
            return self._error(correlation, QueryErrorCode.INTERNAL_ERROR, "Source Item Catalog changed during query")
        return QueryResult(
            status="ok",
            trace_id=correlation.trace_id,
            data={"source_items": items, "count": len(items)},
            provenance=self._provenance(space_id, "knowledge_list", revision_after),
        )
