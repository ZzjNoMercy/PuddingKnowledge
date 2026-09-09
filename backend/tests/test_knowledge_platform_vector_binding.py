from __future__ import annotations

from typing import Any

from knowledge_platform.catalog.vector_binding import audit_vector_catalog_binding


class _Catalog:
    def __init__(self, collections: list[dict[str, Any]]) -> None:
        self._collections = collections

    def list_collections(self, *, space_id: str | None = None) -> list[dict[str, Any]]:
        return [
            item for item in self._collections
            if space_id is None or item.get("space_id") == space_id
        ]


def _collection(**overrides: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": "dataset_kb",
        "space_id": "space_kb",
        "version": "v1",
        "capabilities": ["document_rag_query"],
        "asset_ids": ["asset-a", "asset-b"],
        "provider_bindings": {"document_rag_query": {"dataset_id": "dataset-kb"}},
    }
    result.update(overrides)
    return result


def test_vector_binding_requires_explicit_binding_and_exact_identity_coverage() -> None:
    audit = audit_vector_catalog_binding(
        catalog=_Catalog([_collection()]),
        space_id="space_kb",
        collection_id="dataset_kb",
        collection_version="v1",
        capability="document_rag_query",
        vector_document_ids=["asset-a", "asset-b"],
    )
    assert audit.activation_allowed is True
    assert audit.matched_identity_count == 2

    mismatch = audit_vector_catalog_binding(
        catalog=_Catalog([_collection()]),
        space_id="space_kb",
        collection_id="dataset_kb",
        collection_version="v1",
        capability="document_rag_query",
        vector_document_ids=["milvus-uuid"],
    )
    assert mismatch.activation_allowed is False
    assert mismatch.matched_identity_count == 0


def test_vector_binding_fails_closed_for_missing_catalog_binding_or_collection() -> None:
    missing_binding = audit_vector_catalog_binding(
        catalog=_Catalog([_collection(provider_bindings={})]),
        space_id="space_kb",
        collection_id="dataset_kb",
        collection_version="v1",
        capability="document_rag_query",
        vector_document_ids=["asset-a"],
    )
    assert missing_binding.activation_allowed is False
    missing_collection = audit_vector_catalog_binding(
        catalog=_Catalog([]),
        space_id="space_kb",
        collection_id="dataset_kb",
        collection_version="v1",
        capability="document_rag_query",
        vector_document_ids=["asset-a"],
    )
    assert missing_collection.collection_found is False
    assert missing_collection.activation_allowed is False
