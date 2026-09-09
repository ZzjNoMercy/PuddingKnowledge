from __future__ import annotations

import hashlib

import pytest

from knowledge_platform.catalog.vector_rebuild import build_vector_rebuild_manifest
from knowledge_platform.retrieval.milvus import MilvusCatalogRetrievalProvider, _safe_quote
from knowledge_platform.retrieval.ports import RetrievalIndexNotReady, RetrievalProviderError


def _digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


class _Catalog:
    def __init__(self) -> None:
        self.assets = {
            "asset-a": {
                "id": "asset-a",
                "space_id": "space-kb",
                "source_uri": "knowledge://spaces/space-kb/assets/asset-a",
            }
        }

    def get_asset(self, *, asset_id: str):
        return self.assets.get(asset_id)


class _Client:
    def __init__(self, hits):
        self.hits = hits
        self.calls = []

    def search(self, **kwargs):
        self.calls.append(kwargs)
        return [self.hits]


def _provider(client: _Client) -> MilvusCatalogRetrievalProvider:
    manifest = build_vector_rebuild_manifest(
        catalog_revision=_digest("catalog"),
        collection={
            "id": "collection-kb",
            "space_id": "space-kb",
            "version": "v1",
            "capabilities": ["document_rag_query"],
            "asset_ids": ["asset-a"],
        },
        assets=[{"id": "asset-a", "revision": "rev-a", "content_digest": _digest("a")}],
    )
    return MilvusCatalogRetrievalProvider(
        catalog=_Catalog(),
        client=client,
        collection_name="puddingclaw_platform_candidate_text",
        manifest=manifest,
        encode_query=lambda _: [0.1, 0.2],
    )


@pytest.mark.asyncio
async def test_milvus_provider_returns_only_catalog_bound_candidates() -> None:
    client = _Client([{"id": "chunk-1", "doc_id": "asset-a", "text": "bound text", "distance": 0.8}])
    result = await _provider(client).search(query="hello", space_id="space-kb", limit=5)
    assert result[0].asset_id == "asset-a"
    assert result[0].quote == "bound text"
    assert client.calls[0]["output_fields"] == ["doc_id", "text"]


@pytest.mark.asyncio
async def test_milvus_provider_rejects_legacy_uuid_or_mixed_index_hits() -> None:
    provider = _provider(_Client([{"id": "chunk-1", "doc_id": "legacy-uuid", "text": "wrong"}]))
    with pytest.raises(RetrievalIndexNotReady):
        await provider.search(query="hello", space_id="space-kb", limit=5)


@pytest.mark.asyncio
async def test_milvus_provider_fails_closed_on_bad_query_and_encoder_vector() -> None:
    provider = _provider(_Client([]))
    with pytest.raises(RetrievalProviderError):
        await provider.search(query=" ", space_id="space-kb", limit=5)
    bad = _provider(_Client([]))
    bad._encode_query = lambda _: [float("nan")]
    with pytest.raises(RetrievalProviderError):
        await bad.search(query="hello", space_id="space-kb", limit=5)


def test_milvus_provider_rejects_a_collection_not_named_by_the_manifest() -> None:
    with pytest.raises(ValueError, match="manifest candidate"):
        _provider(_Client([])).__class__(
            catalog=_Catalog(),
            client=_Client([]),
            collection_name="puddingclaw_knowledge_text",
            manifest=_provider(_Client([]))._manifest,
            encode_query=lambda _: [0.1, 0.2],
        )


def test_milvus_quote_redacts_windows_drive_and_unc_paths() -> None:
    quote = _safe_quote(r"drive=C:\Users\pet\private.md unc=\\server\share\private.md")
    assert "C:\\Users" not in quote
    assert "\\\\server" not in quote
    assert quote.count("[redacted-path]") == 2
