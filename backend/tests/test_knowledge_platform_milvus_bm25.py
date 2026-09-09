from __future__ import annotations

import hashlib

import pytest

from knowledge_platform.catalog.vector_rebuild import build_vector_rebuild_manifest
from knowledge_platform.retrieval.milvus import MilvusBm25CatalogRetrievalProvider
from knowledge_platform.retrieval.ports import RetrievalIndexNotReady, RetrievalProviderError


def _digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


class _Catalog:
    def get_asset(self, *, asset_id: str):
        return {
            "id": asset_id,
            "space_id": "space-kb",
            "source_uri": f"knowledge://spaces/space-kb/assets/{asset_id}",
        } if asset_id == "asset-a" else None


class _Client:
    def __init__(self, hits):
        self.hits = hits
        self.calls = []

    def search(self, **kwargs):
        self.calls.append(kwargs)
        return [self.hits]


def _provider(client: _Client) -> MilvusBm25CatalogRetrievalProvider:
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
        provider_collection_name="puddingclaw_platform_candidate_lexical_text",
    )
    return MilvusBm25CatalogRetrievalProvider(
        catalog=_Catalog(),
        client=client,
        collection_name="puddingclaw_platform_candidate_lexical_text",
        manifest=manifest,
    )


@pytest.mark.asyncio
async def test_bm25_provider_uses_query_text_and_catalog_bound_hit() -> None:
    client = _Client([{"id": "chunk_1", "doc_id": "asset-a", "text": "matched", "distance": 3.0}])
    result = await _provider(client).search(query="PuddingClaw", space_id="space-kb", limit=5)
    assert result[0].asset_id == "asset-a"
    assert client.calls[0]["data"] == ["PuddingClaw"]
    assert client.calls[0]["anns_field"] == "sparse_embedding"


@pytest.mark.asyncio
async def test_bm25_provider_rejects_unbound_legacy_hit() -> None:
    provider = _provider(_Client([{"id": "chunk_1", "doc_id": "legacy-uuid", "text": "wrong"}]))
    with pytest.raises(RetrievalIndexNotReady):
        await provider.search(query="hello", space_id="space-kb", limit=5)


@pytest.mark.asyncio
async def test_bm25_provider_normalizes_secret_like_citation_failure() -> None:
    provider = _provider(_Client([{"id": "chunk_1", "doc_id": "asset-a", "text": "api_key: do-not-return"}]))
    with pytest.raises(RetrievalProviderError, match="unsafe citation") as error:
        await provider.search(query="hello", space_id="space-kb", limit=5)
    assert "do-not-return" not in str(error.value)


@pytest.mark.asyncio
async def test_bm25_provider_redacts_physical_path_before_citation() -> None:
    provider = _provider(_Client([{"id": "chunk_1", "doc_id": "asset-a", "text": "located at /Users/pet/private.md"}]))
    result = await provider.search(query="hello", space_id="space-kb", limit=5)
    assert result[0].quote == "located at [redacted-path]"
