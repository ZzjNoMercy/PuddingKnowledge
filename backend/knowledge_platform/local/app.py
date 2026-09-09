"""Compose the local read/query application using owned Platform services."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from knowledge_contracts import Correlation, Principal
from knowledge_platform.catalog import CatalogQueryService, SqliteCatalogQueryRepository
from knowledge_platform.catalog.local_asset_binding_review_queue import LocalAssetBindingReviewQueue
from knowledge_platform.catalog.service import CatalogJobQueryService
from knowledge_platform.retrieval import (
    AssetDerivativeService,
    AssetReadService,
    CatalogSearchService,
    DocumentRetrievalService,
    LocalFilesystemBlobReader,
    LocalPublishedWikiProvider,
    QueryResultArtifactReadService,
    WikiQueryService,
)
from knowledge_platform.router import KnowledgeQueryRouter, build_local_query_engines
from knowledge_platform.transport import (
    McpQueryAdapter,
    RestAdminAdapter,
    RestJobAdapter,
    RestQueryAdapter,
    StaticProcessingBindingResolver,
    create_platform_app,
)


def _build_app(
    repository: SqliteCatalogQueryRepository,
    bindings: dict[str, Path],
    principal: Principal,
    deployment: Any | None = None,
    semantic_markdown: Any | None = None,
    connector_catalog: Any | None = None,
    asset_upload: Any | None = None,
    package_import: Any | None = None,
    index_rebuild: Any | None = None,
    connector_authorization: Any | None = None,
    notifications: Any | None = None,
    asset_binding_review_queue: LocalAssetBindingReviewQueue | None = None,
    query_result_artifact: QueryResultArtifactReadService | None = None,
    database_nl2sql: Any | None = None,
    database_execute: Any | None = None,
    database_schema: Any | None = None,
    table_query: Any | None = None,
    structured_authoring: Any | None = None,
    structured_processing: Any | None = None,
    processing_bindings: Any | None = None,
    processing_worker: Any | None = None,
):
    catalog = CatalogQueryService(repository)
    provider = LocalPublishedWikiProvider(catalog=repository, asset_paths=bindings)
    asset_read = AssetReadService(catalog=repository, reader=LocalFilesystemBlobReader(bindings))
    derivative_bindings = {
        str(asset.get("id")): ("normalized_markdown",)
        for asset in repository.list_assets()
        if str(asset.get("kind") or "") == "wiki_page" and str(asset.get("id")) in bindings
    }
    wiki = WikiQueryService(provider, repository)
    document = DocumentRetrievalService(provider, repository)
    rest = RestQueryAdapter(
        catalog=catalog,
        search=CatalogSearchService(catalog),
        asset_read=asset_read,
        derivatives=AssetDerivativeService(
            catalog=catalog, asset_read=asset_read, bindings=derivative_bindings
        ),
        document=document,
        wiki=wiki,
        database_nl2sql=database_nl2sql,
        database_execute=database_execute,
        database_schema=database_schema,
        table=table_query,
        knowledge_query=KnowledgeQueryRouter(
            catalog=repository,
            engines=build_local_query_engines(wiki=wiki, table=table_query, database_nl2sql=database_nl2sql),
        ),
        deployment=deployment,
    )
    jobs = RestJobAdapter(jobs=CatalogJobQueryService(repository))
    admin = None
    if any(item is not None for item in (semantic_markdown, connector_catalog, asset_upload, package_import, index_rebuild, connector_authorization, notifications, asset_binding_review_queue, structured_authoring, structured_processing)):
        admin = RestAdminAdapter(
            authoring=structured_authoring,
            processing=structured_processing,
            bindings=processing_bindings or StaticProcessingBindingResolver({}),
            processing_worker=processing_worker,
            semantic_markdown=semantic_markdown,
            connector_catalog=connector_catalog,
            asset_upload=asset_upload,
            package_import=package_import,
            index_rebuild=index_rebuild,
            connector_authorization=connector_authorization,
            notifications=notifications,
            asset_binding_review_queue=asset_binding_review_queue,
        )
    return create_platform_app(
        query_adapter=rest,
        admin_adapter=admin,
        job_adapter=jobs,
        mcp_adapter=McpQueryAdapter(rest, query_result_artifact=query_result_artifact, semantic_markdown=semantic_markdown),
        principal_provider=lambda: principal,
        correlation_provider=lambda: Correlation("phase8-local-platform-http-shadow"),
    )
