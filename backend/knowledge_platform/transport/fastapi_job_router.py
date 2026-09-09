"""Optional FastAPI edge for read-only Admin/Processing job observation."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from knowledge_contracts import Correlation, Principal

from .fastapi_router import CorrelationProvider, PrincipalProvider, _resolve
from .job_adapters import RestJobAdapter


def create_job_router(
    adapter: RestJobAdapter,
    *,
    principal_provider: PrincipalProvider,
    correlation_provider: CorrelationProvider,
) -> APIRouter:
    """Create the job observation route; host authentication remains external."""

    router = APIRouter(prefix="/v1", tags=["knowledge-jobs"])

    async def principal() -> Principal:
        return await _resolve(principal_provider())

    async def correlation() -> Correlation:
        return await _resolve(correlation_provider())

    @router.get("/jobs/{job_id}")
    async def read_job(
        job_id: str,
        current_principal: Principal = Depends(principal),
        current_correlation: Correlation = Depends(correlation),
    ):
        return await adapter.handle(
            method="GET",
            path=f"/v1/jobs/{job_id}",
            principal=current_principal,
            correlation=current_correlation,
        )

    return router
