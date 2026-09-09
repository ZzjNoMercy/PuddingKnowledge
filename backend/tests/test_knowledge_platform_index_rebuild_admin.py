from __future__ import annotations

import hashlib
import json
from pathlib import Path

from fastapi.testclient import TestClient

from knowledge_contracts import Correlation, Principal
from knowledge_platform.catalog.index_rebuild import CatalogIndexRebuildService, IndexRebuildRequest
from knowledge_platform.transport import RestAdminAdapter, StaticProcessingBindingResolver, create_platform_app


class _Catalog:
    catalog_revision = "sha256:" + "c" * 64

    def list_collections(self, *, space_id: str | None = None):
        return [
            {
                "id": "collection_1",
                "space_id": space_id or "space_1",
                "version": "v1",
                "capabilities": ["document_rag_query"],
                "asset_ids": ["asset_1"],
            }
        ]

    def list_assets(self, *, space_id: str | None = None):
        return [
            {
                "id": "asset_1",
                "space_id": space_id or "space_1",
                "revision": "revision_1",
                "content_digest": "sha256:" + hashlib.sha256(b"alpha notes").hexdigest(),
            }
        ]


def _principal() -> Principal:
    return Principal(subject_id="index-admin", scopes=("knowledge.admin", "knowledge.space:space_1"))


def _request(**overrides: str) -> IndexRebuildRequest:
    values = {
        "space_id": "space_1",
        "collection_id": "collection_1",
        "collection_version": "v1",
        "capability": "document_rag_query",
        "provider_id": "puddingclaw_platform_candidate_text",
        "idempotency_key": "index-rebuild-1",
    }
    values.update(overrides)
    return IndexRebuildRequest(**values)


def test_index_rebuild_stages_verified_inactive_candidate_and_is_idempotent(tmp_path: Path) -> None:
    source = tmp_path / "source.md"
    source.write_bytes(b"alpha notes")
    service = CatalogIndexRebuildService(
        _Catalog(), source_bindings={"asset_1": source}, output_root=tmp_path / "index-staging"
    )

    first = service.rebuild(principal=_principal(), correlation=Correlation("index-test"), request=_request())
    second = service.rebuild(principal=_principal(), correlation=Correlation("index-test"), request=_request())

    assert first.status == second.status == "ok"
    assert first.data == second.data
    index = first.data["index"]
    assert index["status"] == "candidate_ready"
    assert index["active"] is False
    assert index["activation_allowed"] is False
    assert index["chunk_count"] == 1
    payload = json.dumps(first.to_dict(), ensure_ascii=False)
    assert str(source) not in payload
    assert "knowledge://spaces/space_1/collections/collection_1/index-candidates/" in payload
    candidate_root = tmp_path / "index-staging/candidates" / index["job_id"]
    assert (candidate_root / "rebuild-manifest.json").is_file()
    assert (candidate_root / "chunks.json").is_file()


def test_index_rebuild_rejects_missing_binding_scope_and_provider_identity(tmp_path: Path) -> None:
    source = tmp_path / "source.md"
    source.write_bytes(b"alpha notes")
    service = CatalogIndexRebuildService(
        _Catalog(), source_bindings={}, output_root=tmp_path / "index-staging"
    )
    missing = service.rebuild(principal=_principal(), correlation=Correlation("index-test"), request=_request())
    assert missing.error.code.value == "binding_unavailable"
    denied = service.rebuild(
        principal=Principal(subject_id="denied"), correlation=Correlation("index-test"), request=_request()
    )
    assert denied.error.code.value == "permission_denied"
    wrong_provider = CatalogIndexRebuildService(
        _Catalog(), source_bindings={"asset_1": source}, output_root=tmp_path / "index-staging-2"
    ).rebuild(
        principal=_principal(),
        correlation=Correlation("index-test"),
        request=_request(provider_id="unapproved_provider"),
    )
    assert wrong_provider.error.code.value == "invalid_request"


def test_index_rebuild_fastapi_route_returns_candidate_without_activation(tmp_path: Path) -> None:
    source = tmp_path / "source.md"
    source.write_bytes(b"alpha notes")
    service = CatalogIndexRebuildService(
        _Catalog(), source_bindings={"asset_1": source}, output_root=tmp_path / "index-staging"
    )
    adapter = RestAdminAdapter(
        authoring=object(),
        processing=object(),
        bindings=StaticProcessingBindingResolver({}),
        index_rebuild=service,
    )
    app = create_platform_app(
        query_adapter=object(),
        admin_adapter=adapter,
        principal_provider=_principal,
        correlation_provider=lambda: Correlation("index-http-test"),
    )
    with TestClient(app) as client:
        response = client.post(
            "/v1/indexes:rebuild",
            json={
                "space_id": "space_1",
                "collection_id": "collection_1",
                "collection_version": "v1",
                "capability": "document_rag_query",
                "provider_id": "puddingclaw_platform_candidate_text",
                "idempotency_key": "index-http-1",
            },
        )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["data"]["index"]["activation_allowed"] is False
