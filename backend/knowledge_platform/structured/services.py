"""Application service for bounded structured/table queries."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping

from knowledge_contracts import Correlation, Evidence, Principal, Provenance, QueryError, QueryErrorCode, QueryResult

from .ports import (
    SemanticContextBinding,
    SemanticContextRegistry,
    StructuredAssetCatalog,
    StructuredQueryProvider,
    TableQueryPayload,
)

_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,160}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_QUERY_LENGTH = 512
_MAX_LIMIT = 20
_MAX_RESPONSE_BYTES = 1024 * 1024


def _structured_uri_parts(resource_uri: str) -> tuple[str, str] | None:
    parts = resource_uri.removeprefix("knowledge://").split("/")
    if len(parts) != 5 or parts[0] != "spaces" or parts[2] != "structured-assets" or parts[4] != "source":
        return None
    return parts[1], parts[3]


def _error(correlation: Correlation, code: QueryErrorCode, message: str) -> QueryResult:
    return QueryResult(status="error", trace_id=correlation.trace_id, error=QueryError(code=code, message=message))


def _authorized(principal: Principal, space_id: str | None) -> bool:
    scopes = set(principal.scopes)
    if principal.tenant_id is not None:
        return False
    if not ({"knowledge.table_query", "knowledge:table_query", "knowledge.search", "knowledge:search", "knowledge.admin", "knowledge:admin"} & scopes):
        return False
    if space_id is None:
        return bool({"knowledge.admin", "knowledge:admin"} & scopes)
    return bool({f"knowledge.space:{space_id}", f"knowledge:space:{space_id}", "knowledge.admin", "knowledge:admin"} & scopes)


class TableQueryService:
    """Keep semantic binding, Catalog identity and provider output in one fence."""

    def __init__(
        self,
        *,
        provider: StructuredQueryProvider,
        catalog: StructuredAssetCatalog,
        semantic_registry: SemanticContextRegistry | None = None,
    ) -> None:
        self._provider = provider
        self._catalog = catalog
        self._semantic_registry = semantic_registry

    async def query(
        self,
        *,
        principal: Principal,
        correlation: Correlation,
        query: str,
        asset_id: str | None = None,
        dataset_id: str | None = None,
        space_id: str | None = None,
        limit: int = 5,
        semantic_context: object | None = None,
    ) -> QueryResult:
        if not _authorized(principal, space_id):
            return _error(correlation, QueryErrorCode.PERMISSION_DENIED, "table_query scope is required")
        if type(query) is not str or not query.strip():
            return _error(correlation, QueryErrorCode.INVALID_REQUEST, "query must not be empty")
        if len(query) > _MAX_QUERY_LENGTH:
            return _error(correlation, QueryErrorCode.RESOURCE_LIMIT_EXCEEDED, "query is too long")
        if asset_id is not None and (type(asset_id) is not str or not _ID_RE.fullmatch(asset_id)):
            return _error(correlation, QueryErrorCode.INVALID_REQUEST, "asset_id is invalid")
        if dataset_id is not None and (type(dataset_id) is not str or not _ID_RE.fullmatch(dataset_id)):
            return _error(correlation, QueryErrorCode.INVALID_REQUEST, "dataset_id is invalid")
        if asset_id is not None and dataset_id is not None:
            return _error(correlation, QueryErrorCode.INVALID_REQUEST, "asset_id and dataset_id are mutually exclusive")
        if space_id is not None and (type(space_id) is not str or not _ID_RE.fullmatch(space_id)):
            return _error(correlation, QueryErrorCode.INVALID_REQUEST, "space_id is invalid")
        if type(limit) is not int or not 1 <= limit <= _MAX_LIMIT:
            return _error(correlation, QueryErrorCode.RESOURCE_LIMIT_EXCEEDED, "limit is out of range")
        try:
            semantic = SemanticContextBinding.from_object(semantic_context)
        except ValueError:
            return _error(correlation, QueryErrorCode.INVALID_REQUEST, "semantic context is invalid")
        try:
            selected_asset_id = asset_id
            query_space_id = space_id
            logical_source_asset_ids: tuple[str, ...] = ()
            if asset_id is not None:
                direct_asset = self._catalog.get_structured_asset(asset_id=asset_id)
                if (
                    direct_asset is None
                    or str(direct_asset.get("reference_status") or "") not in {"ready", "verified", "active"}
                    or "table_query" not in {str(item) for item in (direct_asset.get("capabilities") or [])}
                ):
                    return _error(correlation, QueryErrorCode.BINDING_UNAVAILABLE, "structured Asset is not bound")
                direct_space_id = str(direct_asset.get("space_id") or "")
                if query_space_id is not None and query_space_id != direct_space_id:
                    return _error(correlation, QueryErrorCode.PERMISSION_DENIED, "Asset is outside Space scope")
                query_space_id = direct_space_id
            if dataset_id is not None:
                dataset = self._catalog.get_structured_asset(asset_id=dataset_id)
                logical = dataset.get("logical_dataset") if isinstance(dataset, Mapping) else None
                source_asset_ids = logical.get("source_asset_ids") if isinstance(logical, Mapping) else None
                if (
                    dataset is None
                    or not isinstance(logical, Mapping)
                    or logical.get("materialization") not in {"virtual", "snapshot"}
                    or not isinstance(source_asset_ids, list)
                    or not source_asset_ids
                    or any(type(item) is not str or not _ID_RE.fullmatch(item) for item in source_asset_ids)
                    or len(set(source_asset_ids)) != len(source_asset_ids)
                ):
                    return _error(correlation, QueryErrorCode.BINDING_UNAVAILABLE, "logical dataset is not bound")
                dataset_space_id = str(dataset.get("space_id") or "")
                if not _ID_RE.fullmatch(dataset_space_id):
                    return _error(correlation, QueryErrorCode.BINDING_UNAVAILABLE, "logical dataset Space is invalid")
                if (
                    str(dataset.get("reference_status") or "") not in {"ready", "verified", "active"}
                    or "table_query" not in {str(item) for item in (dataset.get("capabilities") or [])}
                ):
                    return _error(correlation, QueryErrorCode.BINDING_UNAVAILABLE, "logical dataset is not approved")
                logical_source_asset_ids = tuple(str(item) for item in source_asset_ids)
                source_records = [
                    self._catalog.get_structured_asset(asset_id=str(source_id)) for source_id in source_asset_ids
                ]
                if any(
                    source is None
                    or str(source.get("space_id") or "") != dataset_space_id
                    or str(source.get("reference_status") or "") not in {"ready", "verified", "active"}
                    or "table_query" not in {str(item) for item in (source.get("capabilities") or [])}
                    for source in source_records
                ):
                    return _error(correlation, QueryErrorCode.BINDING_UNAVAILABLE, "logical dataset source is not bound")
                if query_space_id is not None and query_space_id != dataset_space_id:
                    return _error(correlation, QueryErrorCode.PERMISSION_DENIED, "dataset is outside Space scope")
                query_space_id = dataset_space_id
                selected_asset_id = dataset_id
            if semantic is not None:
                if self._semantic_registry is None:
                    return _error(correlation, QueryErrorCode.BINDING_UNAVAILABLE, "semantic context registry is unavailable")
                registered = self._semantic_registry.resolve(
                    context_id=semantic.context_id,
                    content_hash=semantic.content_hash,
                )
                registered_binding = SemanticContextBinding.from_object(registered)
                if registered_binding != semantic:
                    return _error(correlation, QueryErrorCode.BINDING_UNAVAILABLE, "semantic context is not registered")
            revision_before = str(self._catalog.catalog_revision)
            if not _DIGEST_RE.fullmatch(revision_before):
                return _error(correlation, QueryErrorCode.BINDING_UNAVAILABLE, "Catalog revision is unavailable")
            payloads = tuple(
                await self._provider.query(
                    query=query,
                    asset_id=selected_asset_id,
                    space_id=query_space_id,
                    limit=limit,
                    semantic_context=semantic_context,
                )
            )
            if len(payloads) > limit:
                raise ValueError("provider returned more table results than requested")
            tables: list[dict[str, object]] = []
            evidence: list[Evidence] = []
            for payload in payloads:
                if not isinstance(payload, TableQueryPayload):
                    raise ValueError("provider returned a non-contract table result")
                if selected_asset_id is not None and payload.asset_id != selected_asset_id:
                    raise ValueError("provider returned a different Asset")
                uri_parts = _structured_uri_parts(payload.resource_uri)
                if uri_parts is None or uri_parts[1] != payload.asset_id:
                    raise ValueError("provider returned an invalid Asset URI")
                if query_space_id is not None and uri_parts[0] != query_space_id:
                    raise ValueError("provider returned a different Space")
                if semantic is not None:
                    if semantic.source_asset_ids and payload.asset_id not in semantic.source_asset_ids:
                        semantic_ids = set(semantic.source_asset_ids)
                        if not (
                            dataset_id is not None
                            and logical_source_asset_ids
                            and set(logical_source_asset_ids).issubset(semantic_ids)
                        ):
                            raise ValueError("table result is outside semantic source binding")
                    if payload.semantic_context_id != semantic.context_id or payload.semantic_context_hash != semantic.content_hash:
                        raise ValueError("table result is not bound to semantic context")
                asset = self._catalog.get_structured_asset(asset_id=payload.asset_id)
                if (
                    asset is None
                    or str(asset.get("source_uri") or "") != payload.resource_uri
                    or str(asset.get("space_id") or "") != uri_parts[0]
                    or str(asset.get("content_digest") or "") != payload.content_digest
                    or "table_query" not in {str(item) for item in (asset.get("capabilities") or [])}
                    or str(asset.get("reference_status") or "") not in {"ready", "verified", "active"}
                ):
                    raise ValueError("table result is not bound to Catalog")
                tables.append(
                    {
                        "asset_id": payload.asset_id,
                        "resource_uri": payload.resource_uri,
                        "answer": payload.answer,
                        "columns": list(payload.columns),
                        "preview_rows": [dict(row) for row in payload.preview_rows],
                        "row_count": payload.row_count,
                        "score": payload.score,
                    }
                )
                evidence.append(
                    Evidence(
                        asset_id=payload.asset_id,
                        resource_uri=payload.resource_uri,
                        locator={"section": "table_result"},
                        quote=payload.answer[:1200],
                        score=payload.score,
                        revision=payload.content_digest,
                        matched_by=("asset_id", "table_query"),
                    )
                )
            revision_after = str(self._catalog.catalog_revision)
            if revision_after != revision_before:
                raise ValueError("Catalog changed during table query")
            response_size = len(
                json.dumps({"query": query, "tables": tables}, ensure_ascii=False, separators=(",", ":")).encode(
                    "utf-8"
                )
            )
            if response_size > _MAX_RESPONSE_BYTES:
                raise ValueError("table query response is too large")
        except Exception:
            return _error(correlation, QueryErrorCode.CAPABILITY_UNAVAILABLE, "table query provider is unavailable")
        return QueryResult(
            status="ok",
            trace_id=correlation.trace_id,
            answer=str(tables[0]["answer"]) if tables else "未找到匹配的结构化资产。",
            data={
                "query": query,
                "tables": tables,
                "count": len(tables),
                "limit": limit,
                "semantic_context": {
                    "context_id": semantic.context_id,
                    "content_hash": semantic.content_hash,
                }
                if semantic is not None
                else None,
            },
            evidence=tuple(evidence),
            provenance=Provenance(
                space_id=query_space_id or "catalog",
                dataset_id=dataset_id,
                dataset_version=None,
                capability="table_query",
                catalog_revision=revision_before,
            ),
        )
