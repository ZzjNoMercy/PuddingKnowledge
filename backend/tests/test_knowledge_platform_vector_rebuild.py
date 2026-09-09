from __future__ import annotations

import hashlib

import pytest

from knowledge_platform.catalog.vector_rebuild import (
    VectorRebuildManifest,
    VectorRebuildPlanError,
    build_vector_rebuild_manifest,
)


def _digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def _collection() -> dict[str, object]:
    return {
        "id": "collection-kb",
        "space_id": "space-kb",
        "version": "v1",
        "capabilities": ["document_rag_query"],
        "asset_ids": ["asset-a", "asset-b"],
    }


def _assets() -> list[dict[str, object]]:
    return [
        {"id": "asset-a", "revision": "rev-a", "content_digest": _digest("a")},
        {"id": "asset-b", "revision": "rev-b", "content_digest": _digest("b")},
    ]


def test_rebuild_manifest_uses_catalog_asset_ids_and_is_deterministic() -> None:
    manifest = build_vector_rebuild_manifest(
        catalog_revision=_digest("catalog"),
        collection=_collection(),
        assets=list(reversed(_assets())),
    )
    assert [item.provider_document_id for item in manifest.documents] == ["asset-a", "asset-b"]
    assert manifest.provider_collection_name == "puddingclaw_platform_candidate_text"
    assert manifest.manifest_digest() == build_vector_rebuild_manifest(
        catalog_revision=_digest("catalog"), collection=_collection(), assets=_assets()
    ).manifest_digest()


def test_rebuild_manifest_rejects_missing_asset_or_provider_identity() -> None:
    with pytest.raises(VectorRebuildPlanError, match="missing Catalog assets"):
        build_vector_rebuild_manifest(
            catalog_revision=_digest("catalog"),
            collection=_collection(),
            assets=[_assets()[0]],
        )

    bad = _assets()
    bad[0]["id"] = "other"
    with pytest.raises(VectorRebuildPlanError, match="missing Catalog assets"):
        build_vector_rebuild_manifest(
            catalog_revision=_digest("catalog"), collection=_collection(), assets=bad
        )


def test_rebuild_manifest_rejects_undeclared_capability_and_bad_digest() -> None:
    with pytest.raises(VectorRebuildPlanError, match="capability"):
        build_vector_rebuild_manifest(
            catalog_revision=_digest("catalog"),
            collection={**_collection(), "capabilities": []},
            assets=_assets(),
        )
    bad = _assets()
    bad[0]["content_digest"] = "not-a-digest"
    with pytest.raises(VectorRebuildPlanError, match="digest"):
        build_vector_rebuild_manifest(
            catalog_revision=_digest("catalog"), collection=_collection(), assets=bad
        )


def test_manifest_round_trips_through_strict_path_free_dict_loader() -> None:
    manifest = build_vector_rebuild_manifest(
        catalog_revision=_digest("catalog"), collection=_collection(), assets=_assets()
    )
    assert VectorRebuildManifest.from_dict(manifest.to_dict()) == manifest
