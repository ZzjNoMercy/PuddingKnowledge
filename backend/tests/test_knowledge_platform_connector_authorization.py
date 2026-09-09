from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from knowledge_contracts import Correlation, Principal
from knowledge_platform.catalog.connector_authorization import (
    ConnectorAuthorizationRequest,
    ConnectorAuthorizationService,
)
from knowledge_platform.transport import RestAdminAdapter, StaticProcessingBindingResolver, create_platform_app


class _Catalog:
    catalog_revision = "sha256:" + "a" * 64

    def list_connectors(self, *, space_id: str):
        return [
            {
                "id": "connector_local",
                "space_id": space_id,
                "connector_key": "web_capture",
                "name": "Local capture",
                "credential_ref": "vault://secret/must-not-leak",
            }
        ]

    def list_connector_authorizations(self, *, space_id: str):
        return [
            {
                "connector_id": "connector_local",
                "space_id": space_id,
                "connector_key": "web_capture",
                "name": "Local capture",
                "connector_status": "active",
                "auth_type": "oauth2",
                "grant_status": "active",
                "active_grant_count": 1,
                "oauth_session_status": "",
                "authorization_required": False,
                "grant_updated_at": "2026-09-05T00:00:00+00:00",
                "oauth_session_expires_at": None,
                "token_credential_ref": "credential://must-not-leak",
            }
        ]


def _principal() -> Principal:
    return Principal(subject_id="auth-admin", scopes=("knowledge.admin", "knowledge.space:space_1"))


def _request(**overrides: str) -> ConnectorAuthorizationRequest:
    values = {
        "space_id": "space_1",
        "connector_id": "connector_local",
        "mode": "user_reauthorize",
        "idempotency_key": "connector-auth-1",
    }
    values.update(overrides)
    return ConnectorAuthorizationRequest(**values)


def test_connector_authorization_status_is_redacted_and_intent_is_inactive(tmp_path: Path) -> None:
    service = ConnectorAuthorizationService(_Catalog(), staging_root=tmp_path / "authorization")
    status = service.list_status(
        principal=_principal(), correlation=Correlation("auth-status"), space_id="space_1"
    )
    assert status.status == "ok"
    status_payload = json.dumps(status.to_dict(), ensure_ascii=False)
    assert "credential_ref" not in status_payload
    assert "token_credential_ref" not in status_payload
    assert "vault://" not in status_payload

    first = service.authorize(
        principal=_principal(), correlation=Correlation("auth-start"), request=_request()
    )
    second = service.authorize(
        principal=_principal(), correlation=Correlation("auth-start"), request=_request()
    )
    assert first.status == second.status == "ok"
    assert first.data == second.data
    authorization = first.data["authorization"]
    assert authorization["status"] == "awaiting_host_authorization"
    payload = json.dumps(first.to_dict(), ensure_ascii=False)
    assert "token_credential_ref" not in payload
    assert "access_token" not in payload
    assert "external_url" not in payload
    assert str(tmp_path) not in payload


def test_connector_authorization_enforces_admin_space_and_connector_scope(tmp_path: Path) -> None:
    service = ConnectorAuthorizationService(_Catalog(), staging_root=tmp_path / "authorization")
    denied = service.list_status(
        principal=Principal(subject_id="user", scopes=("knowledge.space:space_1",)),
        correlation=Correlation("auth-denied"),
        space_id="space_1",
    )
    assert denied.error.code.value == "permission_denied"
    wrong_space = service.authorize(
        principal=Principal(
            subject_id="admin", scopes=("knowledge.admin", "knowledge.space:space_1"), tenant_id="tenant_1"
        ),
        correlation=Correlation("auth-wrong-space"),
        request=_request(space_id="space_2"),
    )
    assert wrong_space.error.code.value == "permission_denied"
    wrong_connector = service.authorize(
        principal=_principal(),
        correlation=Correlation("auth-missing-connector"),
        request=_request(connector_id="connector_missing", idempotency_key="connector-auth-2"),
    )
    assert wrong_connector.error.code.value == "not_found"
    malformed = service.authorize(
        principal=_principal(),
        correlation=Correlation("auth-malformed"),
        request=ConnectorAuthorizationRequest(
            space_id="space_1", connector_id="connector_local", mode="user_reauthorize", idempotency_key=123  # type: ignore[arg-type]
        ),
    )
    assert malformed.error.code.value == "invalid_request"


def test_connector_authorization_fastapi_routes_are_host_managed(tmp_path: Path) -> None:
    service = ConnectorAuthorizationService(_Catalog(), staging_root=tmp_path / "authorization")
    adapter = RestAdminAdapter(
        authoring=object(),
        processing=object(),
        bindings=StaticProcessingBindingResolver({}),
        connector_authorization=service,
    )
    app = create_platform_app(
        query_adapter=object(),
        admin_adapter=adapter,
        principal_provider=_principal,
        correlation_provider=lambda: Correlation("auth-http"),
    )
    with TestClient(app) as client:
        status = client.get("/v1/connector-authorizations", params={"space_id": "space_1"})
        intent = client.post(
            "/v1/connectors/connector_local:authorize",
            json={"space_id": "space_1", "mode": "full_replace", "idempotency_key": "connector-http-1"},
        )
    assert status.status_code == 200
    assert status.json()["data"]["authorizations"][0]["authorization_required"] is False
    assert intent.status_code == 200
    assert intent.json()["data"]["authorization"]["status"] == "awaiting_host_authorization"
