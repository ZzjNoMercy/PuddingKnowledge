"""Deterministic, single-engine ``knowledge_query`` routing service."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime, timezone

from knowledge_contracts import Correlation, Principal, QueryError, QueryErrorCode, QueryResult

from .ports import CollectionRoute, CollectionRouteCatalog, KnowledgeQueryEngine, KnowledgeQueryRequest

_CAPABILITIES = ("database_nl2sql", "table_query", "wiki_query", "document_rag_query")
_CAPABILITY_SCOPES = {
    "database_nl2sql": ("knowledge.database_nl2sql", "knowledge:database_nl2sql"),
    "table_query": ("knowledge.table_query", "knowledge:table_query"),
    "wiki_query": ("knowledge.search", "knowledge:search"),
    "document_rag_query": ("knowledge.search", "knowledge:search"),
}
_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")


def _error(correlation: Correlation, code: QueryErrorCode, message: str, *, retryable: bool = False) -> QueryResult:
    return QueryResult(
        status="error",
        trace_id=correlation.trace_id,
        error=QueryError(code=code, message=message, retryable=retryable),
    )


def _has_scope(principal: Principal, scope: str) -> bool:
    return scope in principal.scopes or scope.replace(".", ":") in principal.scopes


def _authorized(principal: Principal, space_id: str | None) -> bool:
    if principal.tenant_id is not None or not _has_scope(principal, "knowledge.query"):
        return False
    if space_id is None:
        return _has_scope(principal, "knowledge.admin")
    return _has_scope(principal, "knowledge.admin") or _has_scope(principal, f"knowledge.space:{space_id}")


def _query_profile(query: str) -> Mapping[str, int]:
    normalized = query.casefold()
    return {
        "database_nl2sql": int(bool(re.search(r"\b(sql|database|db|postgres|mysql)\b|数据库|指标|营收|销售额", normalized))),
        "table_query": int(bool(re.search(r"excel|csv|tsv|spreadsheet|table|表格|统计|汇总|分组|趋势", normalized))),
        "wiki_query": int(bool(re.search(r"wiki|页面|概念|术语|定义|实体关系", normalized))),
        "document_rag_query": int(bool(re.search(r"文档|文件|pdf|markdown|说明|手册|资料", normalized))),
    }


def _fresh_enough(collection: CollectionRoute, request: KnowledgeQueryRequest) -> bool:
    freshness = collection.freshness
    state = str(freshness.get("state", freshness.get("status", "ready"))).casefold()
    if state not in {"ready", "fresh", "available", "active"}:
        return False
    if request.max_age_seconds is None:
        return True
    observed_at = freshness.get("observed_at")
    if not isinstance(observed_at, str):
        return False
    try:
        timestamp = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            return False
        age = (datetime.now(timezone.utc) - timestamp.astimezone(timezone.utc)).total_seconds()
    except (TypeError, ValueError):
        return False
    return 0 <= age <= request.max_age_seconds


def _binding_available(collection: CollectionRoute, capability: str) -> bool:
    binding = collection.provider_bindings.get(capability)
    if capability in {"document_rag_query", "wiki_query"}:
        return binding is None or set(binding) == {"provider_id"}
    if binding is None:
        return False
    if capability == "database_nl2sql":
        return set(binding) == {"dataset_id"}
    return set(binding) in ({"asset_id"}, {"dataset_id"})


class KnowledgeQueryRouter:
    """Select exactly one eligible Collection/Capability and invoke one engine."""

    def __init__(
        self,
        *,
        catalog: CollectionRouteCatalog,
        engines: Mapping[str, KnowledgeQueryEngine],
    ) -> None:
        self._catalog = catalog
        self._engines = dict(engines)

    async def query(
        self,
        *,
        principal: Principal,
        correlation: Correlation,
        request: KnowledgeQueryRequest,
    ) -> QueryResult:
        if not _authorized(principal, request.space_id):
            return _error(correlation, QueryErrorCode.PERMISSION_DENIED, "knowledge.query Space scope is required")
        try:
            records = self._catalog.list_collections(space_id=request.space_id)
            collections = tuple(CollectionRoute.from_record(record) for record in records)
        except Exception:
            return _error(correlation, QueryErrorCode.BINDING_UNAVAILABLE, "Collection routing metadata is unavailable")
        if request.collection_id is not None:
            collections = tuple(item for item in collections if item.collection_id == request.collection_id)
        if request.collection_id is not None and not collections:
            return _error(correlation, QueryErrorCode.NOT_FOUND, "Collection was not found")
        if any(item.space_id != request.space_id for item in collections) and request.space_id is not None:
            return _error(correlation, QueryErrorCode.PERMISSION_DENIED, "Collection is outside Space scope")
        if request.collection_id is not None:
            selected = next((item for item in collections if item.space_id == request.space_id or request.space_id is None), None)
            if selected is None:
                return _error(correlation, QueryErrorCode.PERMISSION_DENIED, "Collection is outside Space scope")
            collections = (selected,)
        profile = _query_profile(request.query)
        candidates: list[tuple[tuple[int, int, int, str, str], CollectionRoute, str]] = []
        for collection in collections:
            if not _fresh_enough(collection, request) or collection.cost_units > request.max_cost_units:
                continue
            for capability in collection.capabilities:
                if request.capability_hint is not None and capability != request.capability_hint:
                    continue
                if capability not in self._engines:
                    continue
                if not _binding_available(collection, capability):
                    continue
                if not (
                    any(_has_scope(principal, scope) for scope in _CAPABILITY_SCOPES[capability])
                    or _has_scope(principal, "knowledge.admin")
                ):
                    continue
                hint_rank = 0 if request.capability_hint == capability else 1
                profile_rank = 0 if profile.get(capability, 0) else 1
                candidates.append(((hint_rank, profile_rank, collection.cost_units, collection.collection_id, collection.version), collection, capability))
        if not candidates:
            return _error(correlation, QueryErrorCode.CAPABILITY_UNAVAILABLE, "No eligible single-engine route is available")
        _, collection, capability = min(candidates, key=lambda item: item[0])
        engine = self._engines[capability]
        try:
            result = await engine.query(
                request=request,
                collection=collection,
                principal=principal,
                correlation=correlation,
            )
            if not isinstance(result, QueryResult):
                raise ValueError("engine returned a non-contract result")
            if result.status == "ok":
                allowed_assets = set(collection.asset_ids)
                if allowed_assets and any(item.asset_id not in allowed_assets for item in result.evidence):
                    raise ValueError("engine returned evidence outside the selected Collection")
                data = dict(result.data)
                data["routing"] = {
                    "collection_id": collection.collection_id,
                    "collection_version": collection.version,
                    "capability": capability,
                    "engine_call_count": 1,
                    "cost_units": collection.cost_units,
                    "fusion": False,
                }
                return QueryResult(
                    status="ok",
                    trace_id=result.trace_id,
                    answer=result.answer,
                    data=data,
                    evidence=result.evidence,
                    provenance=result.provenance,
                    warnings=result.warnings,
                )
            return result
        except Exception:
            return _error(correlation, QueryErrorCode.CAPABILITY_UNAVAILABLE, "Selected query engine is unavailable", retryable=True)
