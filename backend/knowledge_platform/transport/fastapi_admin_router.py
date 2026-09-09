"""Optional FastAPI edge for the Admin/Processing adapter."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends

from knowledge_contracts import Correlation, Principal

from .admin_adapters import RestAdminAdapter
from .fastapi_router import CorrelationProvider, PrincipalProvider, _resolve


def create_admin_router(
    adapter: RestAdminAdapter,
    *,
    principal_provider: PrincipalProvider,
    correlation_provider: CorrelationProvider,
) -> APIRouter:
    """Create Admin routes; host authentication remains the sole auth source."""

    router = APIRouter(prefix="/v1", tags=["knowledge-admin"])

    async def principal() -> Principal:
        return await _resolve(principal_provider())

    async def correlation() -> Correlation:
        return await _resolve(correlation_provider())

    @router.post("/assets:upload")
    async def upload_asset(
        body: dict[str, Any] = Body(default_factory=dict),
        current_principal: Principal = Depends(principal),
        current_correlation: Correlation = Depends(correlation),
    ):
        return await adapter.handle(
            method="POST",
            path="/v1/assets:upload",
            principal=current_principal,
            correlation=current_correlation,
            body=body,
        )

    @router.post("/packages:import")
    async def import_package(
        body: dict[str, Any] = Body(default_factory=dict),
        current_principal: Principal = Depends(principal),
        current_correlation: Correlation = Depends(correlation),
    ):
        return await adapter.handle(
            method="POST",
            path="/v1/packages:import",
            principal=current_principal,
            correlation=current_correlation,
            body=body,
        )

    @router.post("/indexes:rebuild")
    async def rebuild_index(
        body: dict[str, Any] = Body(default_factory=dict),
        current_principal: Principal = Depends(principal),
        current_correlation: Correlation = Depends(correlation),
    ):
        return await adapter.handle(
            method="POST",
            path="/v1/indexes:rebuild",
            principal=current_principal,
            correlation=current_correlation,
            body=body,
        )

    @router.get("/connector-authorizations")
    async def list_connector_authorizations(
        space_id: str,
        current_principal: Principal = Depends(principal),
        current_correlation: Correlation = Depends(correlation),
    ):
        return await adapter.handle(
            method="GET",
            path="/v1/connector-authorizations",
            principal=current_principal,
            correlation=current_correlation,
            body={"space_id": space_id},
        )

    @router.get("/notifications")
    async def list_notifications(
        space_id: str,
        limit: int = 20,
        current_principal: Principal = Depends(principal),
        current_correlation: Correlation = Depends(correlation),
    ):
        return await adapter.handle(
            method="GET",
            path="/v1/notifications",
            principal=current_principal,
            correlation=current_correlation,
            body={"space_id": space_id, "limit": limit},
        )

    @router.get("/asset-binding-reviews")
    async def list_asset_binding_reviews(
        space_id: str,
        current_principal: Principal = Depends(principal),
        current_correlation: Correlation = Depends(correlation),
    ):
        return await adapter.handle(
            method="GET",
            path="/v1/asset-binding-reviews",
            principal=current_principal,
            correlation=current_correlation,
            body={"space_id": space_id},
        )

    @router.post("/connectors/{connector_id}:authorize")
    async def authorize_connector(
        connector_id: str,
        body: dict[str, Any] = Body(default_factory=dict),
        current_principal: Principal = Depends(principal),
        current_correlation: Correlation = Depends(correlation),
    ):
        return await adapter.handle(
            method="POST",
            path=f"/v1/connectors/{connector_id}:authorize",
            principal=current_principal,
            correlation=current_correlation,
            body=body,
        )

    @router.post("/datasets")
    async def create_dataset(
        body: dict[str, Any] = Body(default_factory=dict),
        current_principal: Principal = Depends(principal),
        current_correlation: Correlation = Depends(correlation),
    ):
        return await adapter.handle(
            method="POST",
            path="/v1/datasets",
            principal=current_principal,
            correlation=current_correlation,
            body=body,
        )

    @router.post("/wiki/assets/{asset_id}:compile")
    async def compile_wiki_asset(
        asset_id: str,
        body: dict[str, Any] = Body(default_factory=dict),
        current_principal: Principal = Depends(principal),
        current_correlation: Correlation = Depends(correlation),
    ):
        return await adapter.handle(
            method="POST",
            path=f"/v1/wiki/assets/{asset_id}:compile",
            principal=current_principal,
            correlation=current_correlation,
            body=body,
        )

    @router.post("/captures/assets/{asset_id}:process")
    async def process_capture_asset(
        asset_id: str,
        body: dict[str, Any] = Body(default_factory=dict),
        current_principal: Principal = Depends(principal),
        current_correlation: Correlation = Depends(correlation),
    ):
        return await adapter.handle(
            method="POST",
            path=f"/v1/captures/assets/{asset_id}:process",
            principal=current_principal,
            correlation=current_correlation,
            body=body,
        )

    @router.post("/wiki/assets/{asset_id}:project-gbrain")
    async def project_gbrain_asset(
        asset_id: str,
        body: dict[str, Any] = Body(default_factory=dict),
        current_principal: Principal = Depends(principal),
        current_correlation: Correlation = Depends(correlation),
    ):
        return await adapter.handle(
            method="POST",
            path=f"/v1/wiki/assets/{asset_id}:project-gbrain",
            principal=current_principal,
            correlation=current_correlation,
            body=body,
        )

    @router.post("/sources/{connector_id}:sync")
    async def sync_connector_source(
        connector_id: str,
        body: dict[str, Any] = Body(default_factory=dict),
        current_principal: Principal = Depends(principal),
        current_correlation: Correlation = Depends(correlation),
    ):
        return await adapter.handle(
            method="POST",
            path=f"/v1/sources/{connector_id}:sync",
            principal=current_principal,
            correlation=current_correlation,
            body=body,
        )

    @router.post("/database/guardrails")
    async def create_sql_guardrail(
        body: dict[str, Any] = Body(default_factory=dict),
        current_principal: Principal = Depends(principal),
        current_correlation: Correlation = Depends(correlation),
    ):
        return await adapter.handle(
            method="POST",
            path="/v1/database/guardrails",
            principal=current_principal,
            correlation=current_correlation,
            body=body,
        )

    @router.post("/database/bindings")
    async def bind_database_collection(
        body: dict[str, Any] = Body(default_factory=dict),
        current_principal: Principal = Depends(principal),
        current_correlation: Correlation = Depends(correlation),
    ):
        return await adapter.handle(
            method="POST",
            path="/v1/database/bindings",
            principal=current_principal,
            correlation=current_correlation,
            body=body,
        )

    @router.post("/collections/freshness")
    async def observe_collection_freshness(
        body: dict[str, Any] = Body(default_factory=dict),
        current_principal: Principal = Depends(principal),
        current_correlation: Correlation = Depends(correlation),
    ):
        return await adapter.handle(
            method="POST",
            path="/v1/collections/freshness",
            principal=current_principal,
            correlation=current_correlation,
            body=body,
        )

    @router.get("/semantic-assets")
    async def list_semantic_assets(
        space_id: str,
        status: str | None = None,
        current_principal: Principal = Depends(principal),
        current_correlation: Correlation = Depends(correlation),
    ):
        return await adapter.handle(method="GET", path="/v1/semantic-assets", principal=current_principal, correlation=current_correlation, body={"space_id": space_id, "status": status})

    @router.get("/connectors")
    async def list_connectors(
        space_id: str,
        current_principal: Principal = Depends(principal),
        current_correlation: Correlation = Depends(correlation),
    ):
        return await adapter.handle(
            method="GET",
            path="/v1/connectors",
            principal=current_principal,
            correlation=current_correlation,
            body={"space_id": space_id},
        )

    @router.get("/source-items")
    async def list_source_items(
        space_id: str,
        connector_id: str | None = None,
        current_principal: Principal = Depends(principal),
        current_correlation: Correlation = Depends(correlation),
    ):
        return await adapter.handle(
            method="GET",
            path="/v1/source-items",
            principal=current_principal,
            correlation=current_correlation,
            body={"space_id": space_id, "connector_id": connector_id},
        )

    @router.post("/semantic-assets")
    async def prepare_semantic_asset(
        body: dict[str, Any] = Body(default_factory=dict),
        current_principal: Principal = Depends(principal),
        current_correlation: Correlation = Depends(correlation),
    ):
        return await adapter.handle(method="POST", path="/v1/semantic-assets", principal=current_principal, correlation=current_correlation, body=body)

    @router.post("/semantic-assets/{asset_id}:decision")
    async def decide_semantic_asset(
        asset_id: str,
        body: dict[str, Any] = Body(default_factory=dict),
        current_principal: Principal = Depends(principal),
        current_correlation: Correlation = Depends(correlation),
    ):
        return await adapter.handle(method="POST", path=f"/v1/semantic-assets/{asset_id}:decision", principal=current_principal, correlation=current_correlation, body=body)

    @router.post("/database/guardrails/{guardrail_id}:decision")
    async def decide_sql_guardrail(
        guardrail_id: str,
        body: dict[str, Any] = Body(default_factory=dict),
        current_principal: Principal = Depends(principal),
        current_correlation: Correlation = Depends(correlation),
    ):
        return await adapter.handle(
            method="POST",
            path=f"/v1/database/guardrails/{guardrail_id}:decision",
            principal=current_principal,
            correlation=current_correlation,
            body=body,
        )

    @router.post("/semantic-dimensions")
    async def create_semantic_dimension(
        body: dict[str, Any] = Body(default_factory=dict),
        current_principal: Principal = Depends(principal),
        current_correlation: Correlation = Depends(correlation),
    ):
        return await adapter.handle(
            method="POST",
            path="/v1/semantic-dimensions",
            principal=current_principal,
            correlation=current_correlation,
            body=body,
        )

    @router.post("/semantic-dimensions/jobs/{job_id}:decision")
    async def decide_semantic_dimension_job(
        job_id: str,
        body: dict[str, Any] = Body(default_factory=dict),
        current_principal: Principal = Depends(principal),
        current_correlation: Correlation = Depends(correlation),
    ):
        return await adapter.handle(
            method="POST",
            path=f"/v1/semantic-dimensions/jobs/{job_id}:decision",
            principal=current_principal,
            correlation=current_correlation,
            body=body,
        )

    @router.post("/semantic-dimensions/jobs/{job_id}:process")
    async def process_semantic_dimension(
        job_id: str,
        body: dict[str, Any] = Body(default_factory=dict),
        current_principal: Principal = Depends(principal),
        current_correlation: Correlation = Depends(correlation),
    ):
        return await adapter.handle(
            method="POST",
            path=f"/v1/semantic-dimensions/jobs/{job_id}:process",
            principal=current_principal,
            correlation=current_correlation,
            body=body,
        )

    @router.post("/datasets/{dataset_id}:publish")
    async def publish_dataset(
        dataset_id: str,
        body: dict[str, Any] = Body(default_factory=dict),
        current_principal: Principal = Depends(principal),
        current_correlation: Correlation = Depends(correlation),
    ):
        return await adapter.handle(
            method="POST",
            path=f"/v1/datasets/{dataset_id}:publish",
            principal=current_principal,
            correlation=current_correlation,
            body=body,
        )

    return router
