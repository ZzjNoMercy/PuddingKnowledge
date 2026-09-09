"""Adapters that wire existing local application services into the router."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from knowledge_contracts import Correlation, Principal, QueryError, QueryErrorCode, QueryResult

from .ports import CollectionRoute, KnowledgeQueryEngine, KnowledgeQueryRequest


def _error(correlation: Correlation, code: QueryErrorCode, message: str) -> QueryResult:
    return QueryResult(status="error", trace_id=correlation.trace_id, error=QueryError(code=code, message=message))


class LocalServiceQueryEngine(KnowledgeQueryEngine):
    """Call one existing Platform service with only an explicit binding."""

    def __init__(self, *, capability: str, service: Any, provider_id: str | None = None) -> None:
        self._capability = capability
        self._service = service
        self._provider_id = provider_id

    async def query(
        self,
        *,
        request: KnowledgeQueryRequest,
        collection: CollectionRoute,
        principal: Principal,
        correlation: Correlation,
    ) -> QueryResult:
        if self._capability not in collection.capabilities:
            return _error(correlation, QueryErrorCode.BINDING_UNAVAILABLE, "Collection does not expose this capability")
        binding = collection.provider_bindings.get(self._capability)
        if self._provider_id is not None and binding is None and self._capability in {
            "document_rag_query",
            "wiki_query",
        }:
            return _error(correlation, QueryErrorCode.BINDING_UNAVAILABLE, "Collection provider binding is missing")
        if binding is not None and (
            set(binding) != {"provider_id"}
            or self._provider_id is None
            or binding.get("provider_id") != self._provider_id
        ) and self._capability in {"document_rag_query", "wiki_query"}:
            return _error(correlation, QueryErrorCode.BINDING_UNAVAILABLE, "Collection provider binding is unavailable")
        if self._capability in {"table_query", "database_nl2sql"} and binding is None:
            return _error(correlation, QueryErrorCode.BINDING_UNAVAILABLE, "Collection provider binding is missing")
        if self._capability == "document_rag_query":
            return await self._service.query(
                principal=principal,
                correlation=correlation,
                query=request.query,
                space_id=collection.space_id,
                limit=request.limit,
            )
        if self._capability == "wiki_query":
            return await self._service.query(
                principal=principal,
                correlation=correlation,
                query=request.query,
                space_id=collection.space_id,
                limit=request.limit,
            )
        if self._capability == "table_query":
            assert binding is not None
            return await self._service.query(
                principal=principal,
                correlation=correlation,
                query=request.query,
                asset_id=binding.get("asset_id"),
                dataset_id=binding.get("dataset_id"),
                space_id=collection.space_id,
                limit=request.limit,
            )
        if self._capability == "database_nl2sql":
            assert binding is not None
            dataset_id = binding.get("dataset_id")
            if dataset_id is None:
                return _error(correlation, QueryErrorCode.BINDING_UNAVAILABLE, "Database dataset binding is missing")
            return self._service.generate(
                principal=principal,
                correlation=correlation,
                space_id=collection.space_id,
                dataset_id=dataset_id,
                question=request.query,
                semantic_asset_ids=collection.semantic_asset_ids,
            )
        return _error(correlation, QueryErrorCode.CAPABILITY_UNAVAILABLE, "Unsupported local query engine")


def build_local_query_engines(
    *,
    document: Any | None = None,
    wiki: Any | None = None,
    table: Any | None = None,
    database_nl2sql: Any | None = None,
    document_provider_id: str | None = None,
    wiki_provider_id: str | None = None,
) -> Mapping[str, KnowledgeQueryEngine]:
    """Build only the explicitly supplied local service engines."""

    services = {
        "document_rag_query": (document, document_provider_id),
        "wiki_query": (wiki, wiki_provider_id),
        "table_query": (table, None),
        "database_nl2sql": (database_nl2sql, None),
    }
    return {
        capability: LocalServiceQueryEngine(capability=capability, service=service[0], provider_id=service[1])
        for capability, service in services.items()
        if service[0] is not None
    }
