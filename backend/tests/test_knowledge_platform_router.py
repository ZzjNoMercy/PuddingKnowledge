from __future__ import annotations

from collections.abc import Mapping

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from knowledge_contracts import Correlation, Evidence, Principal, QueryResult
from knowledge_platform.router import (
    CollectionRoute,
    KnowledgeQueryRequest,
    KnowledgeQueryRouter,
    build_local_query_engines,
)
from knowledge_platform.transport import McpQueryAdapter, RestQueryAdapter, create_query_router


class _Catalog:
    def __init__(self, records: list[Mapping[str, object]]) -> None:
        self.records = records

    def list_collections(self, *, space_id: str | None = None):
        if space_id is None:
            return self.records
        return [record for record in self.records if record.get("space_id") == space_id]


class _Engine:
    def __init__(self, capability: str, *, asset_id: str = "asset_1") -> None:
        self.capability = capability
        self.asset_id = asset_id
        self.calls: list[str] = []

    async def query(self, *, request, collection, principal, correlation):
        del principal
        self.calls.append(collection.collection_id)
        return QueryResult(
            status="ok",
            trace_id=correlation.trace_id,
            answer=request.query,
            data={"engine": self.capability},
            evidence=(
                Evidence(
                    asset_id=self.asset_id,
                    resource_uri=f"knowledge://spaces/{collection.space_id}/assets/{self.asset_id}",
                    matched_by=("router",),
                ),
            ),
        )


def _record(
    collection_id: str,
    *,
    capability: str,
    cost_units: int = 1,
    freshness: Mapping[str, object] | None = None,
    asset_ids: list[str] | None = None,
    semantic_asset_ids: list[str] | None = None,
    provider_bindings: Mapping[str, Mapping[str, str]] | None = None,
) -> dict[str, object]:
    return {
        "id": collection_id,
        "space_id": "space_1",
        "version": "v1",
        "capabilities": [capability],
        "asset_ids": asset_ids or ["asset_1"],
        "semantic_asset_ids": semantic_asset_ids or [],
        "freshness": dict(freshness or {"state": "ready"}),
        "cost_units": cost_units,
        "provider_bindings": dict(provider_bindings or {}),
    }


def _principal(*scopes: str) -> Principal:
    return Principal("subject_1", scopes=("knowledge.query", "knowledge.space:space_1", *scopes))


@pytest.mark.asyncio
async def test_router_uses_explicit_hint_and_invokes_one_engine() -> None:
    document = _Engine("document_rag_query")
    database = _Engine("database_nl2sql")
    router = KnowledgeQueryRouter(
        catalog=_Catalog(
            [
                _record("docs", capability="document_rag_query"),
                _record(
                    "db",
                    capability="database_nl2sql",
                    provider_bindings={"database_nl2sql": {"dataset_id": "db_dataset"}},
                ),
            ]
        ),
        engines={"document_rag_query": document, "database_nl2sql": database},
    )

    result = await router.query(
        principal=_principal("knowledge.search", "knowledge.database_nl2sql"),
        correlation=Correlation("trace-1"),
        request=KnowledgeQueryRequest(
            query="按文档里的数据库指标回答",
            space_id="space_1",
            capability_hint="database_nl2sql",
        ),
    )

    assert result.status == "ok"
    assert result.data["routing"]["collection_id"] == "db"
    assert result.data["routing"]["engine_call_count"] == 1
    assert result.data["routing"]["fusion"] is False
    assert database.calls == ["db"]
    assert document.calls == []


@pytest.mark.asyncio
async def test_router_uses_question_profile_and_deterministic_cost_tiebreak() -> None:
    expensive = _Engine("table_query")
    cheap = _Engine("table_query")
    router = KnowledgeQueryRouter(
        catalog=_Catalog(
            [
                _record(
                    "expensive",
                    capability="table_query",
                    cost_units=3,
                    provider_bindings={"table_query": {"asset_id": "structured_expensive"}},
                ),
                _record(
                    "cheap",
                    capability="table_query",
                    cost_units=1,
                    provider_bindings={"table_query": {"asset_id": "structured_cheap"}},
                ),
            ]
        ),
        engines={"table_query": cheap},
    )
    result = await router.query(
        principal=_principal("knowledge.table_query"),
        correlation=Correlation("trace-2"),
        request=KnowledgeQueryRequest(query="统计这个 Excel 表格", space_id="space_1"),
    )
    assert result.status == "ok"
    assert result.data["routing"]["collection_id"] == "cheap"
    assert expensive.calls == []


@pytest.mark.asyncio
async def test_router_rejects_stale_or_over_budget_routes() -> None:
    engine = _Engine("document_rag_query")
    router = KnowledgeQueryRouter(
        catalog=_Catalog(
            [
                _record("stale", capability="document_rag_query", freshness={"state": "stale"}),
                _record("unknown", capability="document_rag_query", freshness={"state": "maybe"}),
                _record("costly", capability="document_rag_query", cost_units=9),
            ]
        ),
        engines={"document_rag_query": engine},
    )
    result = await router.query(
        principal=_principal("knowledge.search"),
        correlation=Correlation("trace-3"),
        request=KnowledgeQueryRequest(query="读取文档", space_id="space_1", max_cost_units=2),
    )
    assert result.status == "error"
    assert result.error is not None and result.error.code.value == "capability_unavailable"
    assert engine.calls == []


@pytest.mark.asyncio
async def test_router_requires_query_and_space_scopes_and_rejects_tenant() -> None:
    engine = _Engine("document_rag_query")
    router = KnowledgeQueryRouter(
        catalog=_Catalog([_record("docs", capability="document_rag_query")]),
        engines={"document_rag_query": engine},
    )
    request = KnowledgeQueryRequest(query="读取文档", space_id="space_1")
    denied = await router.query(
        principal=Principal("subject_1", scopes=("knowledge.search", "knowledge.space:space_1")),
        correlation=Correlation("trace-4"),
        request=request,
    )
    tenant_denied = await router.query(
        principal=Principal("subject_1", scopes=("knowledge.query", "knowledge.search", "knowledge.space:space_1"), tenant_id="tenant_1"),
        correlation=Correlation("trace-5"),
        request=request,
    )
    assert denied.status == tenant_denied.status == "error"
    assert denied.error is not None and denied.error.code.value == "permission_denied"
    assert tenant_denied.error is not None and tenant_denied.error.code.value == "permission_denied"
    assert engine.calls == []


@pytest.mark.asyncio
async def test_router_rejects_evidence_outside_selected_collection() -> None:
    engine = _Engine("document_rag_query", asset_id="asset_outside")
    router = KnowledgeQueryRouter(
        catalog=_Catalog([_record("docs", capability="document_rag_query", asset_ids=["asset_1"])]),
        engines={"document_rag_query": engine},
    )
    result = await router.query(
        principal=_principal("knowledge.search"),
        correlation=Correlation("trace-6"),
        request=KnowledgeQueryRequest(query="读取文档", space_id="space_1"),
    )
    assert result.status == "error"
    assert result.error is not None and result.error.code.value == "capability_unavailable"


def test_router_request_and_collection_contracts_are_bounded() -> None:
    with pytest.raises(ValueError):
        KnowledgeQueryRequest(query="", space_id="space_1")
    with pytest.raises(ValueError):
        KnowledgeQueryRequest(query="读取", capability_hint="unknown")
    with pytest.raises(ValueError):
        KnowledgeQueryRequest(query="读取", max_age_seconds=8 * 24 * 60 * 60)
    with pytest.raises(ValueError):
        from_record = dict(_record("docs", capability="document_rag_query"))
        from_record["provider_bindings"] = {"document_rag_query": "implicit"}
        CollectionRoute.from_record(from_record)
    with pytest.raises(ValueError):
        malformed = dict(_record("docs", capability="document_rag_query"))
        malformed["id"] = 123
        CollectionRoute.from_record(malformed)
    with pytest.raises(ValueError):
        malformed = dict(_record("db", capability="database_nl2sql"))
        malformed["provider_bindings"] = {"database_nl2sql": {"dataset_id": 123}}
        CollectionRoute.from_record(malformed)

    actual_shape = CollectionRoute.from_record(
        {
            "id": "collection_1",
            "space_id": "space_1",
            "version": "v1",
            "capabilities": ["knowledge_list", "knowledge_search", "knowledge_read", "document_rag_query"],
            "asset_ids": ["asset_1"],
            "freshness": {"mode": "snapshot", "source_revision": "local"},
        }
    )
    assert "knowledge_search" in actual_shape.capabilities


@pytest.mark.asyncio
async def test_local_table_engine_requires_explicit_collection_binding() -> None:
    class TableService:
        def __init__(self) -> None:
            self.calls = []

        async def query(self, **kwargs):
            self.calls.append(kwargs)
            return QueryResult(status="ok", trace_id=kwargs["correlation"].trace_id, data={"tables": []})

    service = TableService()
    router = KnowledgeQueryRouter(
        catalog=_Catalog([_record("tables", capability="table_query")]),
        engines=build_local_query_engines(table=service),
    )
    result = await router.query(
        principal=_principal("knowledge.table_query"),
        correlation=Correlation("trace-9"),
        request=KnowledgeQueryRequest(query="统计表格", space_id="space_1"),
    )
    assert result.status == "error"
    assert service.calls == []


@pytest.mark.asyncio
async def test_local_table_engine_passes_only_declared_binding() -> None:
    class TableService:
        async def query(self, **kwargs):
            assert kwargs["asset_id"] == "structured_asset_1"
            assert kwargs["dataset_id"] is None
            return QueryResult(status="ok", trace_id=kwargs["correlation"].trace_id, data={"tables": []})

    record = _record("tables", capability="table_query")
    record["provider_bindings"] = {"table_query": {"asset_id": "structured_asset_1"}}
    router = KnowledgeQueryRouter(
        catalog=_Catalog([record]),
        engines=build_local_query_engines(table=TableService()),
    )
    result = await router.query(
        principal=_principal("knowledge.table_query"),
        correlation=Correlation("trace-10"),
        request=KnowledgeQueryRequest(query="统计表格", space_id="space_1"),
    )
    assert result.status == "ok"


@pytest.mark.asyncio
async def test_local_document_engine_requires_matching_explicit_provider_binding() -> None:
    class DocumentService:
        async def query(self, **kwargs):
            return QueryResult(status="ok", trace_id=kwargs["correlation"].trace_id, data={})

    record = _record("docs", capability="document_rag_query")
    record["provider_bindings"] = {"document_rag_query": {"provider_id": "candidate-a"}}
    router = KnowledgeQueryRouter(
        catalog=_Catalog([record]),
        engines=build_local_query_engines(document=DocumentService(), document_provider_id="candidate-a"),
    )
    result = await router.query(
        principal=_principal("knowledge.search"),
        correlation=Correlation("trace-document-provider"),
        request=KnowledgeQueryRequest(
            query="读取文档",
            space_id="space_1",
            collection_id="docs",
            capability_hint="document_rag_query",
        ),
    )
    assert result.status == "ok"

    mismatched = KnowledgeQueryRouter(
        catalog=_Catalog([record]),
        engines=build_local_query_engines(document=DocumentService(), document_provider_id="candidate-b"),
    )
    rejected = await mismatched.query(
        principal=_principal("knowledge.search"),
        correlation=Correlation("trace-document-provider-mismatch"),
        request=KnowledgeQueryRequest(
            query="读取文档",
            space_id="space_1",
            collection_id="docs",
            capability_hint="document_rag_query",
        ),
    )
    assert rejected.status == "error"
    assert rejected.error is not None and rejected.error.code.value == "binding_unavailable"

    unbound = dict(record)
    unbound["provider_bindings"] = {}
    unbound_router = KnowledgeQueryRouter(
        catalog=_Catalog([unbound]),
        engines=build_local_query_engines(document=DocumentService(), document_provider_id="candidate-a"),
    )
    unbound_result = await unbound_router.query(
        principal=_principal("knowledge.search"),
        correlation=Correlation("trace-document-provider-unbound"),
        request=KnowledgeQueryRequest(
            query="读取文档",
            space_id="space_1",
            collection_id="docs",
            capability_hint="document_rag_query",
        ),
    )
    assert unbound_result.status == "error"
    assert unbound_result.error is not None and unbound_result.error.code.value == "binding_unavailable"


@pytest.mark.asyncio
async def test_local_database_engine_passes_collection_semantic_assets_to_service() -> None:
    class DatabaseService:
        def __init__(self) -> None:
            self.calls = []

        def generate(self, **kwargs):
            self.calls.append(kwargs)
            return QueryResult(status="ok", trace_id=kwargs["correlation"].trace_id, data={})

    service = DatabaseService()
    record = _record(
        "db",
        capability="database_nl2sql",
        semantic_asset_ids=["semantic_revenue"],
        provider_bindings={"database_nl2sql": {"dataset_id": "dataset_sales"}},
    )
    result = await KnowledgeQueryRouter(
        catalog=_Catalog([record]),
        engines=build_local_query_engines(database_nl2sql=service),
    ).query(
        principal=_principal("knowledge.database_nl2sql"),
        correlation=Correlation("trace-semantic-database-route"),
        request=KnowledgeQueryRequest(
            query="数据库指标",
            space_id="space_1",
            collection_id="db",
            capability_hint="database_nl2sql",
        ),
    )
    assert result.status == "ok"
    assert service.calls[0]["semantic_asset_ids"] == ("semantic_revenue",)


@pytest.mark.asyncio
async def test_rest_and_mcp_expose_the_same_single_engine_route() -> None:
    engine = _Engine("document_rag_query")
    router = KnowledgeQueryRouter(
        catalog=_Catalog([_record("docs", capability="document_rag_query")]),
        engines={"document_rag_query": engine},
    )
    rest = RestQueryAdapter(
        catalog=object(),
        search=object(),
        asset_read=object(),
        document=object(),
        wiki=object(),
        knowledge_query=router,
    )
    principal = _principal("knowledge.search")
    correlation = Correlation("trace-7")
    rest_result = await rest.handle(
        method="POST",
        path="/v1/knowledge/query",
        principal=principal,
        correlation=correlation,
        body={"query": "读取文档", "space_id": "space_1"},
    )
    mcp_result = await McpQueryAdapter(rest).call_tool(
        name="knowledge_query",
        arguments={"query": "读取文档", "space_id": "space_1"},
        principal=principal,
        correlation=Correlation("trace-8"),
    )
    assert rest_result["status"] == "ok"
    assert mcp_result["structuredContent"]["status"] == "ok"
    assert rest_result["data"]["routing"]["capability"] == "document_rag_query"
    assert mcp_result["structuredContent"]["data"]["routing"]["engine_call_count"] == 1

    app = FastAPI()
    app.include_router(
        create_query_router(
            rest,
            principal_provider=lambda: principal,
            correlation_provider=lambda: correlation,
        )
    )
    generic_response = TestClient(app).post(
        "/v1/query",
        json={"query": "读取文档", "space_id": "space_1"},
    )
    assert generic_response.status_code == 200
    assert generic_response.json()["data"]["routing"]["capability"] == "document_rag_query"


def test_fastapi_exposes_query_result_metadata_route() -> None:
    class Catalog:
        def read_query_result(self, *, principal, correlation, query_result_id):
            del principal, query_result_id
            return QueryResult(status="ok", trace_id=correlation.trace_id, data={"query_result": {"id": "qr-1"}})

    rest = RestQueryAdapter(
        catalog=Catalog(), search=object(), asset_read=object(), document=object(), wiki=object()
    )
    principal = Principal("subject_1", scopes=("knowledge.read",))
    app = FastAPI()
    app.include_router(create_query_router(rest, principal_provider=lambda: principal, correlation_provider=lambda: Correlation("trace-qr")))
    response = TestClient(app).get("/v1/query-results/qr-1")
    assert response.status_code == 200
    assert response.json()["data"]["query_result"]["id"] == "qr-1"
