from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from knowledge_contracts import Correlation, Principal
from knowledge_platform.catalog.local_asset_binding_review_queue import LocalAssetBindingReviewQueue
from knowledge_platform.transport import RestAdminAdapter, StaticProcessingBindingResolver, create_platform_app

_DIGEST = "sha256:" + "a" * 64
_REVIEW_ID = "sha256:" + "b" * 64


def _queue(space_id: str = "space_1") -> dict[str, object]:
    return {
        "format": "agent-knowledge-platform-local-asset-binding-review-queue/v1",
        "status": "PHASE9_LOCAL_ASSET_BINDING_REVIEW_QUEUE_READY_NOT_ACTIVATABLE",
        "activation": "not-activated",
        "execution_allowed": False,
        "review_manifest": {"sha256": _DIGEST, "status": "REVIEW_REQUIRED"},
        "catalog": {
            "canonical_sha256_before": _DIGEST,
            "canonical_sha256_after": _DIGEST,
            "canonical_unchanged": True,
            "revision": _DIGEST,
            "space_id": space_id,
        },
        "summary": {
            "approval_required": True,
            "ambiguous_candidate_item_count": 0,
            "confirmed_candidate_item_count": 1,
            "matched_candidate_item_count": 1,
            "matched_catalog_asset_count": 1,
            "review_item_count": 3,
            "unique_candidate_digest_count": 1,
        },
        "policy": "This is a review queue only; explicit human approval is required.",
        "items": [
            {
                "approval_required": True,
                "bytes": 12,
                "candidate_count": 1,
                "candidate_selection_required": False,
                "catalog_asset_ids": ["asset_1"],
                "catalog_asset_match_count": 1,
                "catalog_assets": [{"asset_id": "asset_1", "kind": "document", "mime_type": "text/plain", "title": "Local notes"}],
                "content_digest": _DIGEST,
                "review_id": _REVIEW_ID,
            }
        ],
    }


def _adapter(queue_path: Path) -> RestAdminAdapter:
    return RestAdminAdapter(
        authoring=object(),
        processing=object(),
        bindings=StaticProcessingBindingResolver({}),
        asset_binding_review_queue=LocalAssetBindingReviewQueue(queue_path),
    )


def _app(queue_path: Path, principal: Principal):
    return create_platform_app(
        query_adapter=object(),
        admin_adapter=_adapter(queue_path),
        principal_provider=lambda: principal,
        correlation_provider=lambda: Correlation("asset-review-queue-test"),
    )


def test_review_queue_fastapi_route_is_admin_scoped_and_path_free(tmp_path: Path) -> None:
    queue_path = tmp_path / "queue.json"
    queue_path.write_text(json.dumps(_queue()), encoding="utf-8")
    allowed = Principal("admin", scopes=("knowledge.admin", "knowledge.space:space_1"))
    denied = Principal("reader", scopes=("knowledge.query", "knowledge.space:space_1"))

    with TestClient(_app(queue_path, allowed)) as client:
        response = client.get("/v1/asset-binding-reviews?space_id=space_1")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["data"]["asset_binding_review_queue"]["items"][0]["review_id"] == _REVIEW_ID
    assert payload["data"]["asset_binding_review_queue"]["execution_allowed"] is False
    assert str(queue_path) not in response.text

    with TestClient(_app(queue_path, denied)) as client:
        denied_response = client.get("/v1/asset-binding-reviews?space_id=space_1")
    assert denied_response.json()["error"]["code"] == "permission_denied"


def test_review_queue_fails_closed_for_other_space_and_path_fields(tmp_path: Path) -> None:
    queue_path = tmp_path / "queue.json"
    queue_path.write_text(json.dumps(_queue()), encoding="utf-8")
    principal = Principal("admin", scopes=("knowledge.admin", "knowledge.space:space_1"))
    with TestClient(_app(queue_path, principal)) as client:
        wrong_space = client.get("/v1/asset-binding-reviews?space_id=space_2")
    assert wrong_space.json()["error"]["code"] == "capability_unavailable"

    document = _queue()
    document["items"][0]["path"] = "/Users/pet/private.bin"  # type: ignore[index]
    queue_path.write_text(json.dumps(document), encoding="utf-8")
    with TestClient(_app(queue_path, principal)) as client:
        invalid = client.get("/v1/asset-binding-reviews?space_id=space_1")
    assert invalid.json()["error"]["code"] == "capability_unavailable"


def test_review_queue_exposes_ambiguous_candidate_count_without_paths(tmp_path: Path) -> None:
    queue_path = tmp_path / "queue.json"
    document = _queue()
    document["summary"]["ambiguous_candidate_item_count"] = 1  # type: ignore[index]
    document["items"][0]["candidate_count"] = 2  # type: ignore[index]
    document["items"][0]["candidate_selection_required"] = True  # type: ignore[index]
    queue_path.write_text(json.dumps(document), encoding="utf-8")
    principal = Principal("admin", scopes=("knowledge.admin", "knowledge.space:space_1"))

    with TestClient(_app(queue_path, principal)) as client:
        response = client.get("/v1/asset-binding-reviews?space_id=space_1")
    assert response.status_code == 200
    payload = response.json()["data"]["asset_binding_review_queue"]
    assert payload["summary"]["ambiguous_candidate_item_count"] == 1
    assert payload["items"][0]["candidate_count"] == 2
    assert payload["items"][0]["candidate_selection_required"] is True
    assert "path" not in response.text
