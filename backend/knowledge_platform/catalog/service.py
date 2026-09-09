"""Framework-neutral application service for read-only Catalog queries."""

from __future__ import annotations

import re
from collections.abc import Mapping

from knowledge_contracts import (
    Correlation,
    Evidence,
    Principal,
    Provenance,
    QueryError,
    QueryErrorCode,
    QueryResult,
)

from .query import CatalogQueryRepository

_MAX_SEARCH_TEXT = 512
_CATALOG_REVISION_RE = re.compile(r"sha256:[0-9a-f]{64}")
_SPACE_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
_QUERY_RESULT_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
_JOB_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
_NON_PORTABLE_CATALOG_TEXT_RE = re.compile(
    r"(?i)(?:password|api[_ -]?key|secret|token|authorization|cookie|private[_ -]?key|path)\s*[:=]|"
    r"(?:https?://|file:|[A-Za-z]:[\\/]|\\\\|(?:^|[\s(])/(?:[^\s]+)|(?:^|[\s(])~/)"
)


def _public_catalog_text(value: object, fallback: str = "") -> str:
    """Return Catalog labels safe to expose over a process boundary."""

    if not isinstance(value, str) or any(
        ord(character) < 32 and character not in "\t\n\r" for character in value
    ):
        return fallback
    return value if not _NON_PORTABLE_CATALOG_TEXT_RE.search(value) else fallback


class CatalogQueryService:
    """Expose Collection/Asset queries without leaking repository details."""

    def __init__(self, repository: CatalogQueryRepository) -> None:
        self._repository = repository
        self._catalog_revision: str | None = None
        self._refresh_revision()

    def _refresh_revision(self) -> bool:
        try:
            candidate = getattr(self._repository, "catalog_revision", None)
        except Exception:
            candidate = None
        if not isinstance(candidate, str) or not _CATALOG_REVISION_RE.fullmatch(candidate):
            self._catalog_revision = None
            return False
        self._catalog_revision = candidate
        return True

    def _revision_error(self, correlation: Correlation) -> QueryResult | None:
        if not self._refresh_revision():
            return self._error(correlation, QueryErrorCode.INTERNAL_ERROR, "Catalog revision is unavailable")
        return None

    def _revision_changed_error(self, correlation: Correlation, revision: str) -> QueryResult | None:
        if not self._refresh_revision() or self._catalog_revision != revision:
            return self._error(correlation, QueryErrorCode.INTERNAL_ERROR, "Catalog changed during query")
        return None

    @staticmethod
    def _error(correlation: Correlation, code: QueryErrorCode, message: str) -> QueryResult:
        return QueryResult(
            status="error",
            trace_id=correlation.trace_id,
            error=QueryError(code=code, message=message),
        )

    @staticmethod
    def _authorized(principal: Principal, scope: str) -> bool:
        # The local staging schema has no tenant-to-space binding.  Refuse a
        # tenant-scoped principal until that binding is part of the port.
        colon_scope = scope.replace(".", ":")
        return principal.tenant_id is None and (
            scope in principal.scopes
            or colon_scope in principal.scopes
            or "knowledge.admin" in principal.scopes
            or "knowledge:admin" in principal.scopes
        )

    def _provenance(self, *, space_id: str | None, capability: str) -> Provenance:
        return Provenance(
            space_id=space_id or "catalog",
            dataset_id=None,
            dataset_version=None,
            capability=capability,
            catalog_revision=self._catalog_revision,
        )

    @staticmethod
    def _asset_evidence(asset: Mapping[str, object], *, matched_by: tuple[str, ...]) -> Evidence:
        revision = str(asset.get("revision") or "")
        return Evidence(
            asset_id=str(asset["id"]),
            resource_uri=str(asset["source_uri"]),
            revision=revision if revision.startswith("sha256:") else "",
            matched_by=matched_by,
        )

    def list_collections(
        self, *, principal: Principal, correlation: Correlation, space_id: str | None = None
    ) -> QueryResult:
        if not self._authorized(principal, "knowledge.list"):
            return self._error(correlation, QueryErrorCode.PERMISSION_DENIED, "knowledge.list scope is required")
        if space_id is not None and (type(space_id) is not str or not _SPACE_ID_RE.fullmatch(space_id)):
            return self._error(correlation, QueryErrorCode.INVALID_REQUEST, "space_id must be an opaque identifier")
        if revision_error := self._revision_error(correlation):
            return revision_error
        try:
            collections = [dict(item) for item in self._repository.list_collections(space_id=space_id)]
        except Exception:
            return self._error(correlation, QueryErrorCode.INTERNAL_ERROR, "Catalog collection query failed")
        if revision_error := self._revision_changed_error(correlation, self._catalog_revision or ""):
            return revision_error
        return QueryResult(
            status="ok",
            trace_id=correlation.trace_id,
            data={"collections": collections, "count": len(collections)},
            provenance=self._provenance(space_id=space_id, capability="knowledge_list"),
        )

    def list_spaces(self, *, principal: Principal, correlation: Correlation) -> QueryResult:
        if not self._authorized(principal, "knowledge.list"):
            return self._error(correlation, QueryErrorCode.PERMISSION_DENIED, "knowledge.list scope is required")
        if revision_error := self._revision_error(correlation):
            return revision_error
        revision = self._catalog_revision or ""
        try:
            spaces = []
            for item in self._repository.list_spaces():
                public_item = dict(item)
                space_id = str(public_item.get("id") or "")
                public_item["name"] = _public_catalog_text(public_item.get("name"), space_id)
                public_item["description"] = _public_catalog_text(public_item.get("description"))
                spaces.append(public_item)
        except Exception:
            return self._error(correlation, QueryErrorCode.INTERNAL_ERROR, "Catalog Space query failed")
        if revision_error := self._revision_changed_error(correlation, revision):
            return revision_error
        return QueryResult(
            status="ok",
            trace_id=correlation.trace_id,
            data={"spaces": spaces, "count": len(spaces)},
            provenance=self._provenance(space_id=None, capability="knowledge_list"),
        )

    def list_assets(
        self, *, principal: Principal, correlation: Correlation, space_id: str | None = None
    ) -> QueryResult:
        if not self._authorized(principal, "knowledge.list"):
            return self._error(correlation, QueryErrorCode.PERMISSION_DENIED, "knowledge.list scope is required")
        if space_id is not None and (type(space_id) is not str or not _SPACE_ID_RE.fullmatch(space_id)):
            return self._error(correlation, QueryErrorCode.INVALID_REQUEST, "space_id must be an opaque identifier")
        if revision_error := self._revision_error(correlation):
            return revision_error
        revision = self._catalog_revision or ""
        try:
            assets = [dict(item) for item in self._repository.list_assets(space_id=space_id)]
        except Exception:
            return self._error(correlation, QueryErrorCode.INTERNAL_ERROR, "Catalog Asset query failed")
        if revision_error := self._revision_changed_error(correlation, revision):
            return revision_error
        return QueryResult(
            status="ok",
            trace_id=correlation.trace_id,
            data={"assets": assets, "count": len(assets)},
            provenance=self._provenance(space_id=space_id, capability="knowledge_list"),
        )

    def search_assets(
        self,
        *,
        principal: Principal,
        correlation: Correlation,
        text: str,
        space_id: str | None = None,
        limit: int = 20,
    ) -> QueryResult:
        if not self._authorized(principal, "knowledge.search"):
            return self._error(correlation, QueryErrorCode.PERMISSION_DENIED, "knowledge.search scope is required")
        if type(text) is not str or not text.strip():
            return self._error(correlation, QueryErrorCode.INVALID_REQUEST, "search text must not be empty")
        if len(text) > _MAX_SEARCH_TEXT:
            return self._error(correlation, QueryErrorCode.RESOURCE_LIMIT_EXCEEDED, "search text is too long")
        if space_id is not None and (type(space_id) is not str or not _SPACE_ID_RE.fullmatch(space_id)):
            return self._error(correlation, QueryErrorCode.INVALID_REQUEST, "space_id must be an opaque identifier")
        if type(limit) is not int or not 1 <= limit <= 100:
            return self._error(correlation, QueryErrorCode.RESOURCE_LIMIT_EXCEEDED, "limit must be an integer from 1 to 100")
        if revision_error := self._revision_error(correlation):
            return revision_error
        try:
            assets = [dict(item) for item in self._repository.search_assets(text=text, space_id=space_id, limit=limit)]
            needle = text.casefold()
            evidence_items: list[Evidence] = []
            for asset in assets:
                matched_by = tuple(
                    field
                    for field in ("title", "description")
                    if needle in str(asset.get(field) or "").casefold()
                )
                if not matched_by:
                    raise ValueError("repository returned an asset that does not match the query")
                evidence_items.append(self._asset_evidence(asset, matched_by=matched_by))
            evidence = tuple(evidence_items)
        except Exception:
            return self._error(correlation, QueryErrorCode.INTERNAL_ERROR, "Catalog asset search failed")
        if revision_error := self._revision_changed_error(correlation, self._catalog_revision or ""):
            return revision_error
        return QueryResult(
            status="ok",
            trace_id=correlation.trace_id,
            data={"assets": assets, "count": len(assets), "limit": limit},
            evidence=evidence,
            provenance=self._provenance(space_id=space_id, capability="knowledge_search"),
        )

    def read_asset(
        self, *, principal: Principal, correlation: Correlation, asset_id: str
    ) -> QueryResult:
        if not self._authorized(principal, "knowledge.read"):
            return self._error(correlation, QueryErrorCode.PERMISSION_DENIED, "knowledge.read scope is required")
        if type(asset_id) is not str or not asset_id.strip():
            return self._error(correlation, QueryErrorCode.INVALID_REQUEST, "asset_id must not be empty")
        if revision_error := self._revision_error(correlation):
            return revision_error
        try:
            asset = self._repository.get_asset(asset_id=asset_id)
        except Exception:
            return self._error(correlation, QueryErrorCode.INTERNAL_ERROR, "Catalog asset read failed")
        if revision_error := self._revision_changed_error(correlation, self._catalog_revision or ""):
            return revision_error
        if asset is None:
            return self._error(correlation, QueryErrorCode.NOT_FOUND, "Asset was not found")
        try:
            result = dict(asset)
            evidence = self._asset_evidence(result, matched_by=("asset_id",))
        except Exception:
            return self._error(correlation, QueryErrorCode.INTERNAL_ERROR, "Catalog asset read failed")
        return QueryResult(
            status="ok",
            trace_id=correlation.trace_id,
            data={"asset": result},
            evidence=(evidence,),
            provenance=self._provenance(space_id=str(result.get("space_id") or "catalog"), capability="knowledge_read"),
        )

    def read_query_result(
        self, *, principal: Principal, correlation: Correlation, query_result_id: str
    ) -> QueryResult:
        """Return QueryResult metadata; artifact bytes require a separate read binding."""

        if not self._authorized(principal, "knowledge.read"):
            return self._error(correlation, QueryErrorCode.PERMISSION_DENIED, "knowledge.read scope is required")
        if type(query_result_id) is not str or not _QUERY_RESULT_ID_RE.fullmatch(query_result_id):
            return self._error(correlation, QueryErrorCode.INVALID_REQUEST, "query_result_id is invalid")
        if revision_error := self._revision_error(correlation):
            return revision_error
        revision = self._catalog_revision or ""
        try:
            result = self._repository.get_query_result(query_result_id=query_result_id)
        except Exception:
            return self._error(correlation, QueryErrorCode.INTERNAL_ERROR, "Catalog QueryResult read failed")
        if revision_error := self._revision_changed_error(correlation, revision):
            return revision_error
        if result is None:
            return self._error(correlation, QueryErrorCode.NOT_FOUND, "QueryResult was not found")
        try:
            payload = dict(result)
            if payload.get("id") != query_result_id:
                raise ValueError("QueryResult identity mismatch")
            artifact_uri = str(payload.get("artifact_uri") or "")
            if artifact_uri and not artifact_uri.startswith("knowledge://"):
                raise ValueError("QueryResult artifact URI is not portable")
            if any(key in payload for key in ("artifact_path", "path", "sql")):
                raise ValueError("QueryResult contains a physical or raw field")
        except Exception:
            return self._error(correlation, QueryErrorCode.INTERNAL_ERROR, "Catalog QueryResult is not portable")
        return QueryResult(
            status="ok",
            trace_id=correlation.trace_id,
            data={"query_result": payload},
            provenance=self._provenance(space_id="catalog", capability="knowledge_read"),
        )


class CatalogJobQueryService:
    """Expose redacted local job lifecycle metadata on the Admin plane."""

    def __init__(self, repository: CatalogQueryRepository) -> None:
        self._repository = repository

    @staticmethod
    def _has_scope(principal: Principal, *scopes: str) -> bool:
        granted = set(principal.scopes)
        return any(scope in granted or scope.replace(".", ":") in granted for scope in scopes)

    @classmethod
    def _authorized(cls, principal: Principal, job: Mapping[str, object]) -> bool:
        if principal.tenant_id is not None or not cls._has_scope(principal, "knowledge.processing", "knowledge.admin"):
            return False
        if cls._has_scope(principal, "knowledge.admin"):
            return True
        space_id = str(job.get("space_id") or "")
        return not space_id or cls._has_scope(principal, f"knowledge.space:{space_id}")

    def read_job(self, *, principal: Principal, correlation: Correlation, job_id: str) -> QueryResult:
        if not self._has_scope(principal, "knowledge.processing", "knowledge.admin"):
            return QueryResult(
                status="error",
                trace_id=correlation.trace_id,
                error=QueryError(code=QueryErrorCode.PERMISSION_DENIED, message="knowledge.processing scope is required"),
            )
        if type(job_id) is not str or not _JOB_ID_RE.fullmatch(job_id):
            return QueryResult(
                status="error",
                trace_id=correlation.trace_id,
                error=QueryError(code=QueryErrorCode.INVALID_REQUEST, message="job_id is invalid"),
            )
        try:
            job = self._repository.get_job(job_id=job_id)
        except Exception:
            return QueryResult(
                status="error",
                trace_id=correlation.trace_id,
                error=QueryError(code=QueryErrorCode.INTERNAL_ERROR, message="Catalog job read failed"),
            )
        if job is None:
            return QueryResult(
                status="error",
                trace_id=correlation.trace_id,
                error=QueryError(code=QueryErrorCode.NOT_FOUND, message="Job was not found"),
            )
        if not self._authorized(principal, job):
            return QueryResult(
                status="error",
                trace_id=correlation.trace_id,
                error=QueryError(code=QueryErrorCode.PERMISSION_DENIED, message="knowledge Space scope is required"),
            )
        payload = dict(job)
        if payload.get("id") != job_id or any(
            isinstance(value, str) and value.startswith(("/", "file://"))
            for value in payload.values()
        ):
            return QueryResult(
                status="error",
                trace_id=correlation.trace_id,
                error=QueryError(code=QueryErrorCode.INTERNAL_ERROR, message="Catalog job is not portable"),
            )
        return QueryResult(
            status="ok",
            trace_id=correlation.trace_id,
            data={"job": payload},
            provenance=Provenance(
                space_id=str(payload.get("space_id") or "catalog"),
                dataset_id=None,
                dataset_version=None,
                capability="knowledge_read",
                catalog_revision=None,
            ),
        )
