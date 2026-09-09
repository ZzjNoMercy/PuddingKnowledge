"""Composition boundary for an independent Platform FastAPI process."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import FastAPI

from knowledge_contracts import Correlation, Principal

from .admin_adapters import RestAdminAdapter
from .fastapi_admin_router import create_admin_router
from .fastapi_job_router import create_job_router
from .fastapi_mcp_router import create_mcp_router
from .fastapi_router import CorrelationProvider, PrincipalProvider, create_query_router
from .job_adapters import RestJobAdapter
from .query_adapters import McpQueryAdapter, RestQueryAdapter


def create_platform_app(
    *,
    query_adapter: RestQueryAdapter,
    principal_provider: PrincipalProvider | Callable[[], Principal | Awaitable[Principal]],
    correlation_provider: CorrelationProvider | Callable[[], Correlation | Awaitable[Correlation]],
    admin_adapter: RestAdminAdapter | None = None,
    job_adapter: RestJobAdapter | None = None,
    mcp_adapter: McpQueryAdapter | None = None,
    title: str = "Knowledge Platform",
    version: str = "v1",
) -> FastAPI:
    """Compose the independent Platform HTTP edge without choosing host auth.

    The app owns no database connections, credentials, or provider discovery.
    The host supplies adapters and authenticated identity providers; omitting
    ``admin_adapter`` creates a query-only sidecar.
    """

    app = FastAPI(title=title, version=version)
    app.include_router(
        create_query_router(
            query_adapter,
            principal_provider=principal_provider,
            correlation_provider=correlation_provider,
        )
    )
    if admin_adapter is not None:
        app.include_router(
            create_admin_router(
                admin_adapter,
                principal_provider=principal_provider,
                correlation_provider=correlation_provider,
            )
        )
    if job_adapter is not None:
        app.include_router(
            create_job_router(
                job_adapter,
                principal_provider=principal_provider,
                correlation_provider=correlation_provider,
            )
        )
    if mcp_adapter is not None:
        app.include_router(
            create_mcp_router(
                mcp_adapter,
                principal_provider=principal_provider,
                correlation_provider=correlation_provider,
            )
        )
    return app
