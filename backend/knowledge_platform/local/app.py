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
    files: Any | None = None,
    packages: Any | None = None,
    package_config: dict | None = None,
):
    catalog = CatalogQueryService(repository)
    structured_paths = dict(getattr(getattr(table_query, '_provider', None), 'paths', {}))
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
    if files is not None:
        from knowledge_platform.local.files import FileBlobReader
        reader=FileBlobReader(repository,files,reader)
        asset_upload=files
    if packages is not None:
        from knowledge_platform.local.package_import import PackageBlobReader, PackageRetrievalProvider
        from knowledge_platform.local.packages import BoundPackageImport
        from knowledge_platform.local.package_tables import PackageTableProvider
        from knowledge_platform.structured import TableQueryService
        package_import = BoundPackageImport(packages, package_config)
        table_query = TableQueryService(catalog=repository, provider=_CombinedTableProvider(
            repository, PackageTableProvider(packages, repository), table_query._provider if table_query is not None else None))
        reader = PackageBlobReader(repository, packages, reader)
        provider = _CombinedWikiProvider(provider, PackageRetrievalProvider(packages, repository))
    def derivative_targets(asset_id):
        result={}
        for source in (feishu,files,packages):
            if source is not None:result.update(source.derivative_targets(asset_id))
        return result
    asset_read = AssetReadService(catalog=repository, reader=reader)
    derivative_bindings = {
        str(asset.get("id")): ("normalized_markdown",)
        for asset in repository.list_assets()
        if str(asset.get("kind") or "") == "wiki_page" and str(asset.get("id")) in bindings
    }
    wiki = WikiQueryService(provider, repository)
    document_provider=provider
    if files is not None:
        from knowledge_platform.local.file_index import FileIndexProvider
        document_provider=FileIndexProvider(repository,files)
        if packages is not None:
            document_provider = _CombinedWikiProvider(document_provider, PackageRetrievalProvider(packages, repository))
    document = DocumentRetrievalService(document_provider, repository)
    engines = dict(build_local_query_engines(wiki=wiki, table=table_query, database_nl2sql=database_nl2sql,
        document=document if files is not None else None,
        document_provider_id='knowledge_local_files' if files is not None else None))
    if packages is not None:
        for capability in ("wiki_query", "document_rag_query"):
            engines[capability] = _PackageQueryEngine(capability, packages, repository, engines.get(capability))
    rest = RestQueryAdapter(
        catalog=catalog,
        search=CatalogSearchService(catalog),
        asset_read=asset_read,
        derivatives=AssetDerivativeService(
            catalog=catalog, asset_read=asset_read, bindings=derivative_bindings,
            derivative_resolver=derivative_targets if feishu is not None or files is not None or packages is not None else None
        ),
        document=document,
        wiki=wiki,
        database_nl2sql=database_nl2sql,
        database_execute=database_execute,
        database_schema=database_schema,
        table=table_query,
        knowledge_query=KnowledgeQueryRouter(
            catalog=repository,
            engines=engines,
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
    mcp = McpQueryAdapter(rest, query_result_artifact=query_result_artifact, semantic_markdown=semantic_markdown)
    if feishu is not None and any(item['selection']['kind']=='bitable' for item in feishu.config.values()):
        from knowledge_platform.transport.bitable_adapters import BitableMcpQueryAdapter
        mcp = BitableMcpQueryAdapter(rest, bitable=feishu.bitable, query_result_artifact=query_result_artifact, semantic_markdown=semantic_markdown)
    app = create_platform_app(
        query_adapter=rest,
        admin_adapter=admin,
        job_adapter=jobs,
        mcp_adapter=mcp,
        principal_provider=lambda: principal,
        correlation_provider=lambda: Correlation("phase8-local-platform-http-shadow"),
    )
    if read_later is not None:
        from knowledge_platform.transport.fastapi_capture_router import create_capture_router
        app.include_router(create_capture_router(read_later, principal_provider=lambda: principal, wiki_compilation=wiki_compilation))
    if feishu is not None:
        from knowledge_platform.transport.fastapi_feishu_router import create_feishu_router
        app.include_router(create_feishu_router(feishu, principal_provider=lambda: principal))
        from knowledge_platform.transport.fastapi_bitable_router import create_bitable_router
        app.include_router(create_bitable_router(feishu.bitable, principal_provider=lambda: principal))
    if files is not None:
        from knowledge_platform.transport.fastapi_files_router import create_files_router
        app.include_router(create_files_router(files,principal_provider=lambda:principal))
    if packages is not None:
        from knowledge_platform.local.package_export import PackageExportService
        from knowledge_platform.transport.fastapi_packages_router import create_packages_router
        app.include_router(create_packages_router(publisher=packages,
            exporter=PackageExportService(repository, _CombinedBlobReader(
                LocalFilesystemBlobReader(structured_paths), reader, frozenset(structured_paths))), config=package_config,
            principal_provider=lambda: principal))
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


class _PackageQueryEngine:
    """Resolve package bindings separately from installed live providers."""
    def __init__(self, capability, publisher, repository, fallback):
        self.capability, self.publisher = capability, publisher
        self.repository, self.fallback = repository, fallback

    async def query(self, *, request, collection, principal, correlation):
        from knowledge_contracts import QueryError, QueryErrorCode, QueryResult
        binding = collection.provider_bindings.get(self.capability)
        if binding == {"provider_id": "knowledge_package"}:
            from knowledge_platform.local.package_import import PackageRetrievalProvider
            from knowledge_platform.router.local import LocalServiceQueryEngine
            provider = PackageRetrievalProvider(self.publisher, self.repository, asset_ids=collection.asset_ids)
            service = (WikiQueryService(provider, self.repository) if self.capability == "wiki_query"
                       else DocumentRetrievalService(provider, self.repository))
            engine = LocalServiceQueryEngine(capability=self.capability, service=service, provider_id="knowledge_package")
            return await engine.query(request=request, collection=collection, principal=principal, correlation=correlation)
        if self.fallback is not None:
            return await self.fallback.query(request=request, collection=collection, principal=principal, correlation=correlation)
        return QueryResult(status="error", trace_id=correlation.trace_id,
            error=QueryError(code=QueryErrorCode.BINDING_UNAVAILABLE, message="Collection provider binding is unavailable"))


class _CombinedTableProvider:
    def __init__(self, repository, packages, initial):
        self.repository, self.packages, self.initial = repository, packages, initial

    async def query(self, *, query, asset_id, space_id, limit, semantic_context):
        arguments = dict(query=query, asset_id=asset_id, space_id=space_id, limit=limit, semantic_context=semantic_context)
        if asset_id is not None:
            asset = self.repository.get_structured_asset(asset_id=asset_id)
            provider = self.packages if asset is not None and asset.get('source_type') == 'package' else self.initial
            if provider is None:
                return ()
            return await provider.query(**arguments)
        results = list(await self.packages.query(**arguments))
        if self.initial is not None:
            results.extend(await self.initial.query(**arguments))
        results.sort(key=lambda item: (-(item.score or 0), item.asset_id))
        return tuple(results[:limit])


class _CombinedBlobReader:
    def __init__(self, initial, published, initial_ids):
        self.initial, self.published, self.initial_ids = initial, published, initial_ids

    async def read(self, request):
        # Initial bindings are immutable and explicitly approved at bootstrap.
        asset_id = request.resource_uri.rsplit('/', 1)[-1]
        reader = self.initial if asset_id in self.initial_ids else self.published
        return await reader.read(request)
