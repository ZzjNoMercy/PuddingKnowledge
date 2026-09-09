"""Optional FastAPI edge for the framework-neutral Query Plane adapter."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from fastapi import APIRouter, Body, Depends, Query

from knowledge_contracts import Correlation, Principal

from .query_adapters import RestQueryAdapter

PrincipalProvider = Callable[[], Principal | Awaitable[Principal]]
CorrelationProvider = Callable[[], Correlation | Awaitable[Correlation]]


async def _resolve(value: Any) -> Any:
    return await value if hasattr(value, "__await__") else value


def create_query_router(
    adapter: RestQueryAdapter,
    *,
    principal_provider: PrincipalProvider,
    correlation_provider: CorrelationProvider,
) -> APIRouter:
    """Create routes without choosing an auth/token implementation.

    The host application must provide an authenticated Principal.  No
    Authorization header is interpreted here, preventing an unsafe second
    authorization path from growing beside the Platform service.
    """

    router = APIRouter(prefix="/v1", tags=["knowledge-query"])

    async def principal() -> Principal:
        return await _resolve(principal_provider())

    async def correlation() -> Correlation:
        return await _resolve(correlation_provider())

    async def route(
        method: str,
        path: str,
        current_principal: Principal,
        current_correlation: Correlation,
        body: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await adapter.handle(
            method=method,
            path=path,
            principal=current_principal,
            correlation=current_correlation,
            body=body,
        )

    @router.get("/spaces")
    async def spaces(current_principal: Principal = Depends(principal), current_correlation: Correlation = Depends(correlation)):
        return await route("GET", "/v1/spaces", current_principal, current_correlation)

    @router.get("/collections")
    async def collections(
        space_id: str | None = Query(None),
        current_principal: Principal = Depends(principal),
        current_correlation: Correlation = Depends(correlation),
    ):
        return await route(
            "GET",
            "/v1/collections",
            current_principal,
            current_correlation,
            {"space_id": space_id} if space_id is not None else None,
        )

    @router.get("/datasets")
    async def datasets(
        space_id: str | None = Query(None),
        current_principal: Principal = Depends(principal),
        current_correlation: Correlation = Depends(correlation),
    ):
        """Deprecated compatibility alias for the Collection listing."""
        return await route(
            "GET",
            "/v1/datasets",
            current_principal,
            current_correlation,
            {"space_id": space_id} if space_id is not None else None,
        )

    @router.get("/assets")
    async def assets(
        space_id: str | None = Query(None),
        current_principal: Principal = Depends(principal),
        current_correlation: Correlation = Depends(correlation),
    ):
        return await route(
            "GET",
            "/v1/assets",
            current_principal,
            current_correlation,
            {"space_id": space_id} if space_id is not None else None,
        )

    @router.get("/assets/{asset_id}")
    async def asset(asset_id: str, current_principal: Principal = Depends(principal), current_correlation: Correlation = Depends(correlation)):
        return await route("GET", f"/v1/assets/{asset_id}", current_principal, current_correlation)

    @router.get("/assets/{asset_id}/derivatives")
    async def asset_derivatives(
        asset_id: str,
        current_principal: Principal = Depends(principal),
        current_correlation: Correlation = Depends(correlation),
    ):
        return await route("GET", f"/v1/assets/{asset_id}/derivatives", current_principal, current_correlation)

    @router.get("/assets/{asset_id}/derivatives/{kind}")
    async def asset_derivative(
        asset_id: str,
        kind: str,
        start: int = Query(0),
        end: int | None = Query(None),
        expected_digest: str | None = Query(None),
        current_principal: Principal = Depends(principal),
        current_correlation: Correlation = Depends(correlation),
    ):
        body: dict[str, Any] = {"start": start}
        if end is not None:
            body["end"] = end
        if expected_digest is not None:
            body["expected_digest"] = expected_digest
        return await route(
            "GET", f"/v1/assets/{asset_id}/derivatives/{kind}", current_principal, current_correlation, body
        )

    @router.get("/query-results/{query_result_id}")
    async def query_result(
        query_result_id: str,
        current_principal: Principal = Depends(principal),
        current_correlation: Correlation = Depends(correlation),
    ):
        return await route(
            "GET", f"/v1/query-results/{query_result_id}", current_principal, current_correlation
        )

    @router.post("/search")
    async def search(body: dict[str, Any] = Body(default_factory=dict), current_principal: Principal = Depends(principal), current_correlation: Correlation = Depends(correlation)):
        return await route("POST", "/v1/search", current_principal, current_correlation, body)

    @router.post("/assets/{asset_id}:read")
    async def read_asset(asset_id: str, body: dict[str, Any] = Body(default_factory=dict), current_principal: Principal = Depends(principal), current_correlation: Correlation = Depends(correlation)):
        return await route("POST", f"/v1/assets/{asset_id}:read", current_principal, current_correlation, body)

    @router.post("/document-rag/query")
    async def document_query(body: dict[str, Any] = Body(default_factory=dict), current_principal: Principal = Depends(principal), current_correlation: Correlation = Depends(correlation)):
        return await route("POST", "/v1/document-rag/query", current_principal, current_correlation, body)

    @router.post("/wiki/query")
    async def wiki_query(body: dict[str, Any] = Body(default_factory=dict), current_principal: Principal = Depends(principal), current_correlation: Correlation = Depends(correlation)):
        return await route("POST", "/v1/wiki/query", current_principal, current_correlation, body)

    @router.post("/table/query")
    async def table_query(body: dict[str, Any] = Body(default_factory=dict), current_principal: Principal = Depends(principal), current_correlation: Correlation = Depends(correlation)):
        return await route("POST", "/v1/table/query", current_principal, current_correlation, body)

    @router.post("/knowledge/query")
    async def knowledge_query(body: dict[str, Any] = Body(default_factory=dict), current_principal: Principal = Depends(principal), current_correlation: Correlation = Depends(correlation)):
        return await route("POST", "/v1/knowledge/query", current_principal, current_correlation, body)

    @router.post("/query")
    async def query(body: dict[str, Any] = Body(default_factory=dict), current_principal: Principal = Depends(principal), current_correlation: Correlation = Depends(correlation)):
        return await route("POST", "/v1/query", current_principal, current_correlation, body)

    @router.post("/database/nl2sql")
    async def database_nl2sql(body: dict[str, Any] = Body(default_factory=dict), current_principal: Principal = Depends(principal), current_correlation: Correlation = Depends(correlation)):
        return await route("POST", "/v1/database/nl2sql", current_principal, current_correlation, body)

    @router.get("/database/schema")
    async def database_schema(
        space_id: str | None = Query(None),
        dataset_id: str | None = Query(None),
        current_principal: Principal = Depends(principal),
        current_correlation: Correlation = Depends(correlation),
    ):
        return await route(
            "GET",
            "/v1/database/schema",
            current_principal,
            current_correlation,
            {"space_id": space_id, "dataset_id": dataset_id},
        )

    @router.post("/database/query-plans/{query_plan_id}:execute")
    async def database_execute(query_plan_id: str, body: dict[str, Any] = Body(default_factory=dict), current_principal: Principal = Depends(principal), current_correlation: Correlation = Depends(correlation)):
        return await route(
            "POST",
            f"/v1/database/query-plans/{query_plan_id}:execute",
            current_principal,
            current_correlation,
            body,
        )

    return router
