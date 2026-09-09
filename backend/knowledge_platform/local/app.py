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
    wiki_compilation: Any | None = None,
    wiki_provider: Any | None = None,
    wiki_blob_reader: Any | None = None,
    read_later: Any | None = None,
    feishu: Any | None = None,
):
    catalog = CatalogQueryService(repository)
    provider = LocalPublishedWikiProvider(catalog=repository, asset_paths=bindings)
    reader = LocalFilesystemBlobReader(bindings)
    if wiki_provider is not None:
        provider = _CombinedWikiProvider(provider, wiki_provider)
    if wiki_blob_reader is not None:
        reader = _CombinedBlobReader(reader, wiki_blob_reader, frozenset(bindings))
    if feishu is not None:
        from knowledge_platform.local.feishu import FeishuBlobReader
        reader = FeishuBlobReader(repository, feishu, reader)
    if read_later is not None:
        from knowledge_platform.local.read_later import CaptureBlobReader
        reader = CaptureBlobReader(repository, read_later, reader)
    asset_read = AssetReadService(catalog=repository, reader=reader)
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
    if any(item is not None for item in (semantic_markdown, connector_catalog, asset_upload, package_import, index_rebuild, connector_authorization, notifications, asset_binding_review_queue, structured_authoring, structured_processing, wiki_compilation)):
        admin = RestAdminAdapter(
            wiki_compilation=wiki_compilation,
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
    app = create_platform_app(
        query_adapter=rest,
        admin_adapter=admin,
        job_adapter=jobs,
        mcp_adapter=McpQueryAdapter(rest, query_result_artifact=query_result_artifact, semantic_markdown=semantic_markdown),
        principal_provider=lambda: principal,
        correlation_provider=lambda: Correlation("phase8-local-platform-http-shadow"),
    )
    if read_later is not None:
        from knowledge_platform.transport.fastapi_capture_router import create_capture_router
        app.include_router(create_capture_router(read_later, principal_provider=lambda: principal, wiki_compilation=wiki_compilation))
    if feishu is not None:
        from knowledge_platform.transport.fastapi_feishu_router import create_feishu_router
        app.include_router(create_feishu_router(feishu, principal_provider=lambda: principal))
    return app


class _CombinedWikiProvider:
    def __init__(self, initial, published):
        self.initial, self.published = initial, published

    async def search(self, *, query, space_id, limit):
        current = tuple(await self.published.search(query=query, space_id=space_id, limit=limit))
        if len(current) >= limit:
            return current[:limit]
        initial = await self.initial.search(query=query, space_id=space_id, limit=limit)
        seen = {item.asset_id for item in current}
        return (current + tuple(item for item in initial if item.asset_id not in seen))[:limit]


class _CombinedBlobReader:
    def __init__(self, initial, published, initial_ids):
        self.initial, self.published, self.initial_ids = initial, published, initial_ids

    async def read(self, request):
        # Initial bindings are immutable and explicitly approved at bootstrap.
        asset_id = request.resource_uri.rsplit('/', 1)[-1]
        reader = self.initial if asset_id in self.initial_ids else self.published
        return await reader.read(request)
