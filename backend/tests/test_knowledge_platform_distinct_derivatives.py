from __future__ import annotations

import base64
import hashlib

import pytest

from knowledge_contracts import BlobReadResult, Correlation, Evidence, Principal, QueryError, QueryErrorCode, QueryResult
from knowledge_platform.retrieval import AssetDerivativeService, AssetReadService


def _digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _principal() -> Principal:
    return Principal("local-agent", scopes=("knowledge.read", "knowledge.space:space_1"))


class Catalog:
    catalog_revision = _digest(b"catalog")

    def __init__(self, assets: dict[str, dict[str, object]]) -> None:
        self.assets = assets

    def get_asset(self, *, asset_id: str):
        return self.assets.get(asset_id)

    def read_asset(self, *, principal, correlation, asset_id):
        if "knowledge.read" not in principal.scopes:
            return QueryResult(status="error", trace_id=correlation.trace_id, error=QueryError(QueryErrorCode.PERMISSION_DENIED, "denied"))
        asset = self.assets.get(asset_id)
        if asset is None:
            return QueryResult(status="error", trace_id=correlation.trace_id, error=QueryError(QueryErrorCode.NOT_FOUND, "missing"))
        evidence = Evidence(asset_id=asset_id, resource_uri=str(asset["source_uri"]), revision=str(asset["content_digest"]), matched_by=("asset_id",))
        return QueryResult(status="ok", trace_id=correlation.trace_id, data={"asset": dict(asset)}, evidence=(evidence,))


class Reader:
    def __init__(self, files: dict[str, bytes], before_return=None) -> None:
        self.files = files
        self.before_return = before_return

    async def read(self, request):
        if self.before_return:
            self.before_return()
        content = self.files[request.resource_uri]
        full_digest = _digest(content)
        return BlobReadResult(
            resource_uri=request.resource_uri,
            content=content[request.start:request.end],
            content_digest=_digest(content[request.start:request.end]),
            asset_digest=full_digest,
            start=request.start,
            end=min(len(content), request.end),
        )


def _services(*, resolver, assets, files, before_return=None):
    catalog = Catalog(assets)
    read = AssetReadService(catalog=catalog, reader=Reader(files, before_return=before_return))
    return AssetDerivativeService(catalog=catalog, asset_read=read, bindings={}, derivative_resolver=resolver)


def _assets(*, target_space="space_1"):
    pdf = b"%PDF original"
    markdown = b"# normalized markdown\n"
    return {
        "pdf": {
            "id": "pdf", "space_id": "space_1", "source_uri": "knowledge://spaces/space_1/assets/pdf",
            "revision": _digest(pdf), "content_digest": _digest(pdf), "mime_type": "application/pdf",
        },
        "md": {
            "id": "md", "space_id": target_space, "source_uri": "knowledge://spaces/{}/assets/md".format(target_space),
            "revision": _digest(markdown), "content_digest": _digest(markdown), "mime_type": "text/markdown",
        },
    }, {"knowledge://spaces/space_1/assets/md": markdown, "knowledge://spaces/space_2/assets/md": markdown}


@pytest.mark.asyncio
async def test_dynamic_derivative_reads_distinct_target_bytes_and_metadata() -> None:
    assets, files = _assets()
    service = _services(resolver=lambda asset_id: {"normalized_markdown": "md"}, assets=assets, files=files)
    listed = service.list(principal=_principal(), correlation=Correlation("list"), asset_id="pdf")
    item = listed.data["derivatives"][0]
    assert item["mime_type"] == "text/markdown"
    assert item["content_digest"] == assets["md"]["content_digest"]
    assert item["content_digest"] != assets["pdf"]["content_digest"]
    result = await service.read(principal=_principal(), correlation=Correlation("read"), asset_id="pdf", kind="normalized_markdown")
    assert result.status == "ok"
    assert base64.b64decode(result.data["content_base64"]) == b"# normalized markdown\n"
    assert result.data["resource_uri"].endswith("/assets/pdf/derivatives/normalized_markdown")
    assert result.evidence[0].asset_id == "pdf"
    assert result.evidence[0].resource_uri == result.data["resource_uri"]


@pytest.mark.asyncio
async def test_dynamic_derivative_rejects_cross_space_target_and_unauthorized_target() -> None:
    assets, files = _assets(target_space="space_2")
    service = _services(resolver=lambda asset_id: {"normalized_markdown": "md"}, assets=assets, files=files)
    listed = service.list(principal=_principal(), correlation=Correlation("cross-space"), asset_id="pdf")
    assert listed.error.code == QueryErrorCode.BINDING_UNAVAILABLE
    read = await service.read(principal=_principal(), correlation=Correlation("cross-space-read"), asset_id="pdf", kind="normalized_markdown")
    assert read.error.code == QueryErrorCode.BINDING_UNAVAILABLE

    class Unauthorized(Catalog):
        def read_asset(self, *, principal, correlation, asset_id):
            if asset_id == "md":
                return QueryResult(status="error", trace_id=correlation.trace_id, error=QueryError(QueryErrorCode.PERMISSION_DENIED, "denied"))
            return super().read_asset(principal=principal, correlation=correlation, asset_id=asset_id)

    catalog = Unauthorized(assets)
    read_service = AssetReadService(catalog=catalog, reader=Reader(files))
    service = AssetDerivativeService(catalog=catalog, asset_read=read_service, bindings={}, derivative_resolver=lambda _: {"normalized_markdown": "md"})
    assert service.list(principal=_principal(), correlation=Correlation("unauthorized"), asset_id="pdf").error.code == QueryErrorCode.BINDING_UNAVAILABLE


@pytest.mark.asyncio
async def test_dynamic_derivative_fails_if_mapping_changes_during_read() -> None:
    assets, files = _assets()
    state = {"revoked": False}

    def resolver(_asset_id):
        return {} if state["revoked"] else {"normalized_markdown": "md"}

    service = _services(resolver=resolver, assets=assets, files=files, before_return=lambda: state.__setitem__("revoked", True))
    result = await service.read(principal=_principal(), correlation=Correlation("revoke"), asset_id="pdf", kind="normalized_markdown")
    assert result.error.code == QueryErrorCode.INTERNAL_ERROR


@pytest.mark.asyncio
async def test_legacy_tuple_bindings_remain_compatible() -> None:
    assets, files = _assets()
    catalog = Catalog(assets)
    service = AssetDerivativeService(
        catalog=catalog,
        asset_read=AssetReadService(catalog=catalog, reader=Reader({"knowledge://spaces/space_1/assets/pdf": b"%PDF original"})),
        bindings={"pdf": ("original",)},
    )
    result = await service.read(principal=_principal(), correlation=Correlation("legacy"), asset_id="pdf", kind="original")
    assert result.status == "ok"
