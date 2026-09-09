from __future__ import annotations

import base64
import hashlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from knowledge_contracts import (
    BlobReadRequest,
    BlobReadResult,
    CitationCandidate,
    Correlation,
    Principal,
    QueryErrorCode,
    QueryResult,
)
from knowledge_platform.catalog import CatalogQueryService
from knowledge_platform.retrieval import (
    AssetDerivativeService,
    AssetReadService,
    CatalogSearchService,
    DocumentRetrievalService,
    LocalDocumentRetrievalProvider,
    LocalFilesystemBlobReader,
    LocalFilesystemQueryResultBlobReader,
    LocalPublishedWikiProvider,
    QueryResultArtifactReadService,
    RetrievalIndexNotReady,
    RetrievalProviderError,
    WikiQueryService,
)
from knowledge_platform.retrieval.local import _portable_quote
from knowledge_platform.transport import McpQueryAdapter, RestQueryAdapter, create_query_router


def _principal(*scopes: str, tenant_id: str | None = None) -> Principal:
    return Principal("local-agent", scopes=scopes, tenant_id=tenant_id)


def _correlation() -> Correlation:
    return Correlation("trace_retrieval")


class _Reader:
    async def read(self, request):
        content = b"hello"
        return BlobReadResult(
            resource_uri=request.resource_uri,
            content=content,
            content_digest="sha256:" + hashlib.sha256(content).hexdigest(),
            start=request.start,
            end=request.start + len(content),
        )


class _BadReader:
    async def read(self, request):
        return BlobReadResult(
            resource_uri=request.resource_uri,
            content=b"hello",
            content_digest="sha256:" + "0" * 64,
            start=request.start,
            end=request.start + 5,
        )


class _QueryResultScopeReader:
    def __init__(self, space_id: str | None = "space_1") -> None:
        self.space_id = space_id

    def get_space_id(self, *, query_result_id):
        return self.space_id


class _Catalog:
    catalog_revision = "sha256:" + "c" * 64

    def get_asset(self, *, asset_id):
        content = b"hello"
        return {
            "id": asset_id,
            "space_id": "space_1",
            "source_uri": "knowledge://spaces/space_1/assets/asset_1",
            "content_digest": "sha256:" + hashlib.sha256(content).hexdigest(),
            "revision": "sha256:" + "a" * 64,
            "title": "Alpha",
            "description": "fixture",
        }

    def list_spaces(self):
        return [{"id": "space_1", "name": "Local", "description": ""}]

    def list_collections(self, *, space_id=None):
        return [{"id": "collection_1", "space_id": "space_1", "version": "v1"}]

    def list_assets(self, *, space_id=None):
        return [self.get_asset(asset_id="asset_1")]

    def search_assets(self, *, text, space_id, limit):
        return [self.get_asset(asset_id="asset_1")] if text.casefold() in "alpha fixture" else []

    def get_query_result(self, *, query_result_id):
        return {
            "id": query_result_id,
            "status": "ready",
            "question": "alpha",
            "sql_digest": "sha256:" + "1" * 64,
            "columns": ["name"],
            "row_count": 1,
            "profile": {},
            "artifact_uri": f"knowledge://query-results/{query_result_id}/artifact",
            "artifact_reference_digest": "sha256:" + "2" * 64,
            "artifact_format": "jsonl",
            "correlation": {},
            "created_at": "2026-09-05T00:00:00Z",
            "expires_at": "2026-09-06T00:00:00Z",
        }


class _Provider:
    def __init__(self, candidates=None, error=None):
        self.candidates = candidates or []
        self.error = error

    async def search(self, *, query, space_id, limit):
        if self.error:
            raise self.error
        return self.candidates[:limit]


@pytest.mark.asyncio
async def test_asset_read_is_bounded_verified_and_json_safe() -> None:
    service = AssetReadService(catalog=_Catalog(), reader=_Reader())

    result = await service.read(
        principal=_principal("knowledge.read", "knowledge.space:space_1"),
        correlation=_correlation(),
        resource_uri="knowledge://spaces/space_1/assets/asset_1",
        start=0,
        end=5,
    )

    assert result.status == "ok"
    assert result.data["content_base64"] == base64.b64encode(b"hello").decode("ascii")
    assert result.evidence[0].asset_id == "asset_1"
    assert result.provenance.capability == "knowledge_read"


@pytest.mark.asyncio
async def test_asset_derivatives_require_explicit_binding_and_preserve_portable_uri() -> None:
    catalog = CatalogQueryService(_Catalog())
    asset_read = AssetReadService(catalog=_Catalog(), reader=_Reader())
    service = AssetDerivativeService(
        catalog=catalog,
        asset_read=asset_read,
        bindings={"asset_1": ("normalized_markdown",)},
    )

    listed = service.list(
        principal=_principal("knowledge.read", "knowledge.space:space_1"),
        correlation=_correlation(),
        asset_id="asset_1",
    )
    read = await service.read(
        principal=_principal("knowledge.read", "knowledge.space:space_1"),
        correlation=_correlation(),
        asset_id="asset_1",
        kind="normalized_markdown",
        start=0,
        end=5,
    )
    unbound = await service.read(
        principal=_principal("knowledge.read", "knowledge.space:space_1"),
        correlation=_correlation(),
        asset_id="asset_1",
        kind="original",
        start=0,
        end=5,
    )

    assert listed.status == "ok"
    assert listed.data["derivatives"][0]["resource_uri"].endswith("/derivatives/normalized_markdown")
    assert read.status == "ok"
    assert read.data["resource_uri"].endswith("/derivatives/normalized_markdown")
    assert read.evidence[0].resource_uri == read.data["resource_uri"]
    assert unbound.error is not None and unbound.error.code == QueryErrorCode.NOT_FOUND


@pytest.mark.asyncio
async def test_asset_read_rejects_unbounded_and_invalid_provider_output() -> None:
    service = AssetReadService(catalog=_Catalog(), reader=_BadReader())
    common = {
        "principal": _principal("knowledge.read", "knowledge.space:space_1"),
        "correlation": _correlation(),
        "resource_uri": "knowledge://spaces/space_1/assets/asset_1",
        "start": 0,
    }

    unbounded = await service.read(**common)
    invalid_provider = await service.read(**common, end=5)

    assert unbounded.error.code == QueryErrorCode.INVALID_REQUEST
    assert invalid_provider.error.code == QueryErrorCode.INTERNAL_ERROR


@pytest.mark.asyncio
async def test_asset_read_requires_catalog_uri_and_space_binding() -> None:
    result = await AssetReadService(catalog=_Catalog(), reader=_Reader()).read(
        principal=_principal("knowledge.admin"),
        correlation=_correlation(),
        resource_uri="knowledge://spaces/space_2/assets/asset_1",
        end=5,
    )

    assert result.error.code == QueryErrorCode.NOT_FOUND


@pytest.mark.asyncio
async def test_query_result_artifact_requires_explicit_binding_and_full_digest(tmp_path) -> None:
    content = b'{"brand":"local"}\n'
    artifact = tmp_path / "query-result.jsonl"
    artifact.write_bytes(content)
    artifact_uri = "knowledge://query-results/query_result_qr_1/artifact"

    class QueryResultCatalog(_Catalog):
        def get_query_result(self, *, query_result_id):
            result = super().get_query_result(query_result_id=query_result_id)
            result["artifact_uri"] = artifact_uri
            result["profile"] = {"_artifact_sha256": "sha256:" + hashlib.sha256(content).hexdigest()}
            return result

    reader = LocalFilesystemQueryResultBlobReader({artifact_uri: artifact})
    service = QueryResultArtifactReadService(
        catalog=QueryResultCatalog(), reader=reader, scope_reader=_QueryResultScopeReader()
    )
    result = await service.read(
        principal=_principal("knowledge.read", "knowledge.space:space_1"),
        correlation=_correlation(),
        query_result_id="query_result_qr_1",
        resource_uri=artifact_uri,
        end=len(content),
    )
    assert result.status == "ok"
    assert result.data["artifact_digest"].endswith(hashlib.sha256(content).hexdigest())
    assert result.evidence[0].resource_uri == artifact_uri
    assert result.provenance.space_id == "space_1"

    wrong_space = await service.read(
        principal=_principal("knowledge.read", "knowledge.space:space_2"),
        correlation=_correlation(),
        query_result_id="query_result_qr_1",
        resource_uri=artifact_uri,
        end=len(content),
    )
    assert wrong_space.error is not None and wrong_space.error.code == QueryErrorCode.PERMISSION_DENIED

    changed = artifact.read_bytes() + b"changed"
    artifact.write_bytes(changed)
    failed = await service.read(
        principal=_principal("knowledge.read", "knowledge.space:space_1"),
        correlation=_correlation(),
        query_result_id="query_result_qr_1",
        resource_uri=artifact_uri,
        end=len(changed),
    )
    assert failed.error is not None and failed.error.code == QueryErrorCode.INTERNAL_ERROR

    unbound = QueryResultArtifactReadService(
        catalog=QueryResultCatalog(),
        reader=LocalFilesystemQueryResultBlobReader({}),
        scope_reader=_QueryResultScopeReader(None),
    )
    missing = await unbound.read(
        principal=_principal("knowledge.read"),
        correlation=_correlation(),
        query_result_id="query_result_qr_1",
        resource_uri=artifact_uri,
        end=len(content),
    )
    assert missing.error is not None and missing.error.code == QueryErrorCode.BINDING_UNAVAILABLE

    unavailable = QueryResultArtifactReadService(catalog=QueryResultCatalog(), reader=reader)
    no_scope = await unavailable.read(
        principal=_principal("knowledge.read"),
        correlation=_correlation(),
        query_result_id="query_result_qr_1",
        resource_uri=artifact_uri,
        end=len(content),
    )
    assert no_scope.error is not None and no_scope.error.code == QueryErrorCode.BINDING_UNAVAILABLE
    unavailable_mcp = McpQueryAdapter(object(), query_result_artifact=unavailable)
    assert not any("query-results" in item["uriTemplate"] for item in unavailable_mcp.resource_templates())

    artifact.write_bytes(content)
    mcp = McpQueryAdapter(object(), query_result_artifact=service)
    assert any("query-results" in item["uriTemplate"] for item in mcp.resource_templates())
    artifact_read = await mcp.read_resource(
        resource_uri=artifact_uri,
        principal=_principal("knowledge.read", "knowledge.space:space_1"),
        correlation=_correlation(),
        end=len(content),
    )
    assert artifact_read["structuredContent"]["status"] == "ok"
    assert artifact_read["contents"][0]["blob"] == base64.b64encode(content).decode("ascii")


@pytest.mark.asyncio
async def test_document_and_wiki_services_normalize_evidence_and_map_index_state() -> None:
    candidate = CitationCandidate(
        asset_id="asset_1",
        resource_uri="knowledge://spaces/space_1/assets/asset_1",
        quote="alpha",
        locator={"chunk_id": "chunk_1"},
        score=0.9,
    )
    provider = _Provider([candidate, candidate])

    document = await DocumentRetrievalService(provider, _Catalog()).query(
        principal=_principal("knowledge.search", "knowledge.space:space_1"),
        correlation=_correlation(),
        query="alpha",
        space_id="space_1",
    )
    wiki = await WikiQueryService(provider, _Catalog()).query(
        principal=_principal("knowledge.search"),
        correlation=_correlation(),
        query="alpha",
    )
    not_ready = await DocumentRetrievalService(_Provider(error=RetrievalIndexNotReady()), _Catalog()).query(
        principal=_principal("knowledge.search"),
        correlation=_correlation(),
        query="alpha",
    )

    assert document.status == "ok"
    assert len(document.evidence) == 1
    assert document.provenance.capability == "document_rag_query"
    assert wiki.provenance.capability == "wiki_query"
    assert not_ready.error.code == QueryErrorCode.INDEX_NOT_READY


@pytest.mark.asyncio
async def test_retrieval_rejects_cross_space_candidates_and_read_only_search_scope() -> None:
    cross_space = CitationCandidate(
        asset_id="asset_2",
        resource_uri="knowledge://spaces/space_2/assets/asset_2",
        quote="alpha",
    )
    service = DocumentRetrievalService(_Provider([cross_space]), _Catalog())

    leaked = await service.query(
        principal=_principal("knowledge.search", "knowledge.space:space_1"),
        correlation=_correlation(),
        query="alpha",
        space_id="space_1",
    )
    read_only = await service.query(
        principal=_principal("knowledge.read", "knowledge.space:space_1"),
        correlation=_correlation(),
        query="alpha",
    )

    assert leaked.error.code == QueryErrorCode.INTERNAL_ERROR
    assert read_only.error.code == QueryErrorCode.PERMISSION_DENIED


@pytest.mark.asyncio
async def test_asset_read_requires_catalog_content_digest() -> None:
    class CatalogWithoutDigest(_Catalog):
        def get_asset(self, *, asset_id):
            result = super().get_asset(asset_id=asset_id)
            result.pop("content_digest")
            return result

    result = await AssetReadService(catalog=CatalogWithoutDigest(), reader=_Reader()).read(
        principal=_principal("knowledge.read", "knowledge.space:space_1"),
        correlation=_correlation(),
        resource_uri="knowledge://spaces/space_1/assets/asset_1",
        end=5,
    )

    assert result.error.code == QueryErrorCode.BINDING_UNAVAILABLE


@pytest.mark.asyncio
async def test_local_blob_reader_returns_chunk_and_full_asset_digest(tmp_path) -> None:
    path = tmp_path / "asset.md"
    path.write_bytes(b"0123456789")
    reader = LocalFilesystemBlobReader({"asset_1": path})
    request = BlobReadRequest(
        resource_uri="knowledge://spaces/space_1/assets/asset_1",
        principal=_principal("knowledge.read"),
        correlation=_correlation(),
        start=3,
        end=7,
    )

    result = await reader.read(request)

    assert result.content == b"3456"
    assert result.content_digest == "sha256:" + hashlib.sha256(b"3456").hexdigest()
    assert result.asset_digest == "sha256:" + hashlib.sha256(b"0123456789").hexdigest()


@pytest.mark.asyncio
async def test_local_blob_reader_rejects_symlinked_parent_and_traversal(tmp_path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "asset.md").write_text("outside", encoding="utf-8")
    root = tmp_path / "root"
    root.mkdir()
    (root / "linked").symlink_to(outside, target_is_directory=True)
    reader = LocalFilesystemBlobReader({"asset_1": root / "linked" / "asset.md"})
    request = BlobReadRequest(
        resource_uri="knowledge://spaces/space_1/assets/asset_1",
        principal=_principal("knowledge.read"),
        correlation=_correlation(),
        end=7,
    )

    with pytest.raises(RetrievalProviderError, match="symlink"):
        await reader.read(request)

    traversal_reader = LocalFilesystemBlobReader({"asset_1": root / ".." / "outside" / "asset.md"})
    with pytest.raises(RetrievalProviderError, match="unsafe"):
        await traversal_reader.read(request)


@pytest.mark.asyncio
async def test_local_document_provider_is_deterministic_and_path_explicit(tmp_path) -> None:
    path = tmp_path / "asset.md"
    path.write_text("Alpha local document", encoding="utf-8")
    local_digest = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()

    class LocalCatalog(_Catalog):
        def list_assets(self, *, space_id=None):
            asset = dict(super().list_assets(space_id=space_id)[0])
            asset["content_digest"] = local_digest
            return [asset]

    provider = LocalDocumentRetrievalProvider(catalog=LocalCatalog(), asset_paths={"asset_1": path})

    candidates = await provider.search(query="alpha", space_id="space_1", limit=5)

    assert len(candidates) == 1
    assert candidates[0].asset_id == "asset_1"
    assert candidates[0].locator["chunk_id"].startswith("local:")


@pytest.mark.asyncio
async def test_local_document_provider_rejects_catalog_digest_mismatch(tmp_path) -> None:
    path = tmp_path / "asset.md"
    path.write_text("Alpha local document", encoding="utf-8")
    provider = LocalDocumentRetrievalProvider(catalog=_Catalog(), asset_paths={"asset_1": path})

    with pytest.raises(RetrievalProviderError, match="digest"):
        await provider.search(query="alpha", space_id="space_1", limit=5)


@pytest.mark.asyncio
async def test_local_document_provider_does_not_quote_binary_source_bytes(tmp_path) -> None:
    path = tmp_path / "asset.pdf"
    path.write_bytes(b"%PDF-1.7\x00\x01binary")
    digest = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()

    class BinaryCatalog(_Catalog):
        def list_assets(self, *, space_id=None):
            asset = dict(super().list_assets(space_id=space_id)[0])
            asset.update(title="Alpha PDF", content_digest=digest)
            return [asset]

    provider = LocalDocumentRetrievalProvider(catalog=BinaryCatalog(), asset_paths={"asset_1": path})
    candidates = await provider.search(query="alpha", space_id="space_1", limit=5)

    assert candidates[0].quote == "Alpha PDF\nfixture\n"
    assert "binary" not in candidates[0].quote


@pytest.mark.asyncio
async def test_local_document_provider_redacts_non_portable_links_from_quotes(tmp_path) -> None:
    path = tmp_path / "linked.md"
    path.write_text("Google Agent Skills\nsource_url: https://example.test/article", encoding="utf-8")
    digest = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()

    class LinkedCatalog(_Catalog):
        def list_assets(self, *, space_id=None):
            asset = dict(super().list_assets(space_id=space_id)[0])
            asset["content_digest"] = digest
            return [asset]

    provider = LocalDocumentRetrievalProvider(catalog=LinkedCatalog(), asset_paths={"asset_1": path})
    candidates = await provider.search(query="Google Agent Skills", space_id="space_1", limit=5)

    assert candidates[0].quote.endswith("source_url: [external-link]")
    assert "https://" not in candidates[0].quote


def test_local_quote_redacts_windows_drive_and_unc_paths() -> None:
    quote = _portable_quote(
        r"drive=C:\Users\pet\private.md unc=\\server\share\private.md"
    )
    assert "C:\\Users" not in quote
    assert "\\\\server" not in quote
    assert quote.count("[local-reference]") == 2


@pytest.mark.asyncio
async def test_local_published_wiki_provider_filters_non_wiki_assets(tmp_path) -> None:
    path = tmp_path / "published.md"
    path.write_text("Published Wiki Alpha", encoding="utf-8")
    digest = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()

    class MixedCatalog(_Catalog):
        def list_assets(self, *, space_id=None):
            document = dict(super().list_assets(space_id=space_id)[0])
            document["content_digest"] = digest
            wiki = dict(document)
            wiki.update(
                id="wiki_asset",
                kind="wiki_page",
                source_uri="knowledge://spaces/space_1/assets/wiki_asset",
                title="Published Wiki",
            )
            return [document, wiki]

    provider = LocalPublishedWikiProvider(
        catalog=MixedCatalog(),
        asset_paths={"asset_1": path, "wiki_asset": path},
    )

    candidates = await provider.search(query="alpha", space_id="space_1", limit=5)

    assert [item.asset_id for item in candidates] == ["wiki_asset"]


@pytest.mark.asyncio
async def test_retrieval_rejects_tenant_scope_and_bad_queries() -> None:
    service = DocumentRetrievalService(_Provider(), _Catalog())
    tenant = await service.query(
        principal=_principal("knowledge.search", tenant_id="tenant_1"),
        correlation=_correlation(),
        query="alpha",
    )
    empty = await service.query(
        principal=_principal("knowledge.search"),
        correlation=_correlation(),
        query=" ",
    )

    assert tenant.error.code == QueryErrorCode.PERMISSION_DENIED
    assert empty.error.code == QueryErrorCode.INVALID_REQUEST


def test_catalog_search_service_marks_portal_metadata_channel() -> None:
    expected = QueryResult(status="ok", trace_id="trace_retrieval", data={"assets": [], "count": 0})

    class Catalog:
        def search_assets(self, **kwargs):
            return expected

    result = CatalogSearchService(Catalog()).search_portal(
        principal=_principal("knowledge.search"),
        correlation=_correlation(),
        text="alpha",
    )

    assert result.status == "ok"
    assert result.data["channel"] == "metadata"


def test_catalog_search_service_rejects_nonportable_metadata() -> None:
    class Catalog:
        def search_assets(self, **kwargs):
            return QueryResult(
                status="ok",
                trace_id="trace_retrieval",
                data={"assets": [{"id": "asset_1", "description": "token=secret"}], "count": 1},
            )

    result = CatalogSearchService(Catalog()).search_portal(
        principal=_principal("knowledge.search"),
        correlation=_correlation(),
        text="alpha",
    )

    assert result.error.code == QueryErrorCode.INTERNAL_ERROR


@pytest.mark.asyncio
async def test_rest_and_mcp_adapters_share_query_services() -> None:
    repository = _Catalog()
    catalog = CatalogQueryService(repository)
    asset_read = AssetReadService(catalog=repository, reader=_Reader())
    rest = RestQueryAdapter(
        catalog=catalog,
        search=CatalogSearchService(catalog),
        asset_read=asset_read,
        document=DocumentRetrievalService(_Provider([]), repository),
        wiki=WikiQueryService(_Provider([]), repository),
        derivatives=AssetDerivativeService(
            catalog=catalog,
            asset_read=asset_read,
            bindings={"asset_1": ("normalized_markdown",)},
        ),
    )
    mcp = McpQueryAdapter(rest)

    datasets = await rest.handle(
        method="GET",
        path="/v1/datasets",
        principal=_principal("knowledge.list"),
        correlation=_correlation(),
    )
    collections = await rest.handle(
        method="GET",
        path="/v1/collections",
        principal=_principal("knowledge.list"),
        correlation=_correlation(),
    )
    search = await mcp.call_tool(
        name="knowledge_search",
        arguments={"query": "alpha"},
        principal=_principal("knowledge.search"),
        correlation=_correlation(),
    )
    read = await mcp.read_resource(
        resource_uri="knowledge://spaces/space_1/assets/asset_1",
        principal=_principal("knowledge.read", "knowledge.space:space_1"),
        correlation=_correlation(),
        end=5,
    )

    assert datasets["status"] == "ok"
    assert collections["status"] == "ok"
    assert search["structuredContent"]["status"] == "ok"
    assert read["structuredContent"]["status"] == "ok"
    assert read["contents"][0]["blob"] == base64.b64encode(b"hello").decode("ascii")
    derivative_read = await mcp.read_resource(
        resource_uri="knowledge://spaces/space_1/assets/asset_1/derivatives/normalized_markdown",
        principal=_principal("knowledge.read", "knowledge.space:space_1"),
        correlation=_correlation(),
        end=5,
    )
    assert derivative_read["structuredContent"]["status"] == "ok"
    assert derivative_read["contents"][0]["uri"].endswith("/derivatives/normalized_markdown")
    resources = await mcp.list_resources(
        principal=_principal("knowledge.list", "knowledge.space:space_1"),
        correlation=_correlation(),
    )
    assert resources["resources"][0]["uri"] == "knowledge://spaces/space_1/manifest"
    assert "/knowledge/" not in resources["resources"][0]["description"]
    assert len(resources["resourceTemplates"]) == 4
    manifest = await mcp.read_resource(
        resource_uri="knowledge://spaces/space_1/manifest",
        principal=_principal("knowledge.list", "knowledge.space:space_1"),
        correlation=_correlation(),
    )
    assert manifest["structuredContent"]["data"]["resource"]["id"] == "space_1"
    assert "/knowledge/" not in manifest["structuredContent"]["data"]["resource"]["description"]
    dataset = await mcp.read_resource(
        resource_uri="knowledge://spaces/space_1/datasets/collection_1",
        principal=_principal("knowledge.list", "knowledge.space:space_1"),
        correlation=_correlation(),
    )
    assert dataset["structuredContent"]["data"]["resource"]["id"] == "collection_1"
    collection = await mcp.read_resource(
        resource_uri="knowledge://spaces/space_1/collections/collection_1",
        principal=_principal("knowledge.list", "knowledge.space:space_1"),
        correlation=_correlation(),
    )
    assert collection["structuredContent"]["data"]["resource"]["id"] == "collection_1"
    assert collection["structuredContent"]["data"]["resource"]["description"] == "Platform Collection manifest"
    assert any(item["uri"].endswith("/collections/collection_1") for item in resources["resources"])
    denied_manifest = await mcp.read_resource(
        resource_uri="knowledge://spaces/space_1/manifest",
        principal=_principal("knowledge.read", "knowledge.space:space_1"),
        correlation=_correlation(),
    )
    assert denied_manifest["structuredContent"]["error"]["code"] == QueryErrorCode.PERMISSION_DENIED
    assert {item["name"] for item in mcp.tool_descriptors()} >= {"knowledge_read", "wiki_query"}

    app = FastAPI()
    app.include_router(
        create_query_router(
            rest,
            principal_provider=lambda: _principal("knowledge.list"),
            correlation_provider=_correlation,
        )
    )
    response = TestClient(app).get("/v1/spaces")
    assert response.status_code == 200
    assert response.json()["data"]["spaces"][0]["id"] == "space_1"


@pytest.mark.asyncio
async def test_mcp_database_schema_resource_is_bounded_and_template_is_capability_gated() -> None:
    class DatabaseSchemaRest:
        database_schema_available = True

        async def handle(self, *, method, path, principal, correlation, body=None):
            assert method == "GET"
            assert path == "/v1/database/schema"
            if body["dataset_id"] == "dataset_1":
                return {
                    "status": "ok",
                    "trace_id": correlation.trace_id,
                    "data": {
                        "source_revision": "sha256:" + "a" * 64,
                        "tables": [
                            {
                                "table_name": "vehicle_model_base",
                                "columns": ["energy_type", "model_count"],
                                "schema_revision": "sha256:" + "b" * 64,
                            }
                        ],
                    },
                }
            return {
                "status": "error",
                "trace_id": correlation.trace_id,
                "error": {"code": "permission_denied", "message": "denied"},
            }

    mcp = McpQueryAdapter(DatabaseSchemaRest())
    assert any("databases/{dataset_id}/schema/{table_name}" in item["uriTemplate"] for item in mcp.resource_templates())

    readable = await mcp.read_resource(
        resource_uri="knowledge://spaces/space_1/databases/dataset_1/schema/vehicle_model_base",
        principal=_principal("knowledge.database_schema", "knowledge.space:space_1"),
        correlation=_correlation(),
    )
    assert readable["structuredContent"]["status"] == "ok"
    assert readable["contents"][0]["mimeType"] == "application/json"
    assert readable["structuredContent"]["data"]["resource"]["columns"] == ["energy_type", "model_count"]

    unknown_table = await mcp.read_resource(
        resource_uri="knowledge://spaces/space_1/databases/dataset_1/schema/secret_table",
        principal=_principal("knowledge.database_schema", "knowledge.space:space_1"),
        correlation=_correlation(),
    )
    assert unknown_table["structuredContent"]["error"]["code"] == "not_found"

    unavailable = await McpQueryAdapter(object()).read_resource(
        resource_uri="knowledge://spaces/space_1/databases/dataset_1/schema/vehicle_model_base",
        principal=_principal("knowledge.database_schema", "knowledge.space:space_1"),
        correlation=_correlation(),
    )
    assert unavailable["structuredContent"]["error"]["code"] == "binding_unavailable"
