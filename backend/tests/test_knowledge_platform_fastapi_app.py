from __future__ import annotations

from fastapi.testclient import TestClient

from knowledge_contracts import Correlation, Principal
from knowledge_platform.transport import RestAdminAdapter, StaticProcessingBindingResolver, create_platform_app


class _Adapter:
    async def handle(self, *, method, path, principal, correlation, body=None):
        del method, principal, body
        return {"status": "ok", "trace_id": correlation.trace_id, "path": path}


class _Mcp:
    @staticmethod
    def tool_descriptors():
        return ({"name": "knowledge_query", "inputSchema": {"type": "object"}},)

    async def call_tool(self, **kwargs):
        del kwargs
        return {"structuredContent": {"status": "ok"}}

    async def read_resource(self, **kwargs):
        del kwargs
        return {"contents": []}


def _principal() -> Principal:
    return Principal("sidecar-test", scopes=("knowledge.query", "knowledge.admin"))


def test_platform_app_mounts_query_and_admin_edges() -> None:
    app = create_platform_app(
        query_adapter=_Adapter(),
        admin_adapter=RestAdminAdapter(
            authoring=object(),
            processing=object(),
            bindings=StaticProcessingBindingResolver({}),
        ),
        mcp_adapter=_Mcp(),
        principal_provider=_principal,
        correlation_provider=lambda: Correlation("sidecar-http"),
    )

    client = TestClient(app)
    query = client.get("/v1/spaces")
    assert query.status_code == 200
    assert query.json()["path"] == "/v1/spaces"
    schema = client.get("/v1/database/schema?space_id=space-1&dataset_id=dataset-1")
    assert schema.status_code == 200
    assert schema.json()["path"] == "/v1/database/schema"
    admin = client.post("/v1/collections/freshness", json={})
    assert admin.status_code == 200
    assert admin.json()["error"]["code"] == "capability_unavailable"
    mcp = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert mcp.status_code == 200
    assert mcp.json()["result"]["tools"][0]["name"] == "knowledge_query"


def test_platform_app_can_be_query_only() -> None:
    app = create_platform_app(
        query_adapter=_Adapter(),
        principal_provider=_principal,
        correlation_provider=lambda: Correlation("sidecar-query-only"),
    )

    client = TestClient(app)
    assert client.get("/v1/spaces").json()["status"] == "ok"
    assert "/v1/collections/freshness" not in app.openapi()["paths"]
    assert "/mcp" not in app.openapi()["paths"]
