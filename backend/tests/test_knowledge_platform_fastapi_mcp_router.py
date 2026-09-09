from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_mcp_adapters.client import MultiServerMCPClient

from knowledge_contracts import Correlation, Principal
from knowledge_platform.transport import create_mcp_router


class _Mcp:
    @staticmethod
    def tool_descriptors():
        return ({"name": "knowledge_query", "inputSchema": {"type": "object"}},)

    @staticmethod
    def resource_templates():
        return (
            {
                "uriTemplate": "knowledge://spaces/{space_id}/assets/{asset_id}",
                "name": "Asset",
                "mimeType": "text/plain",
            },
        )

    async def call_tool(self, *, name, arguments, principal, correlation):
        assert name == "knowledge_query"
        assert arguments == {"query": "Wiki"}
        assert principal.subject_id == "mcp-test"
        return {"structuredContent": {"status": "ok", "trace_id": correlation.trace_id}}

    async def read_resource(self, *, resource_uri, principal, correlation, start, end):
        if resource_uri != "knowledge://spaces/space_1/assets/asset_1":
            return {
                "contents": [],
                "structuredContent": {
                    "status": "error",
                    "error": {"code": "not_found", "message": "resource not found"},
                },
            }
        assert principal.subject_id == "mcp-test"
        assert start == 0
        assert end >= 4
        if "knowledge:space:space_1" not in principal.scopes:
            return {
                "contents": [],
                "structuredContent": {
                    "status": "error",
                    "error": {"code": "permission_denied", "message": "scope required"},
                },
            }
        return {
            "contents": [{"uri": resource_uri, "mimeType": "text/plain", "text": "resource"}]
        }

    async def list_resources(self, *, principal, correlation):
        assert principal.subject_id == "mcp-test"
        return {
            "resources": [
                {
                    "uri": "knowledge://spaces/space_1/manifest",
                    "name": "Space manifest",
                    "mimeType": "application/json",
                }
            ],
            "resourceTemplates": [{"uriTemplate": "knowledge://spaces/{space_id}/assets/{asset_id}"}],
            "trace_id": correlation.trace_id,
        }


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(
        create_mcp_router(
            _Mcp(),
            principal_provider=lambda: Principal(
                "mcp-test", ("knowledge:read", "knowledge:space:space_1")
            ),
            correlation_provider=lambda: Correlation("mcp-http"),
        )
    )
    return app


def test_mcp_json_rpc_edge_maps_initialize_tools_call_and_resource_read() -> None:
    client = TestClient(_app())

    initialize = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert initialize.json()["result"]["serverInfo"]["name"] == "knowledge-platform"

    tools = client.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert tools.json()["result"]["tools"][0]["name"] == "knowledge_query"

    resources = client.post("/mcp", json={"jsonrpc": "2.0", "id": 2.5, "method": "resources/list"})
    assert resources.json()["result"]["resources"][0]["uri"].endswith("/manifest")
    assert resources.json()["result"]["resourceTemplates"]

    templates = client.post(
        "/mcp", json={"jsonrpc": "2.0", "id": 2.6, "method": "resources/templates/list"}
    )
    assert templates.json()["result"]["resourceTemplates"][0]["uriTemplate"].endswith(
        "/assets/{asset_id}"
    )

    called = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "knowledge_query", "arguments": {"query": "Wiki"}},
        },
    )
    assert called.json()["result"]["structuredContent"]["status"] == "ok"
    assert called.json()["result"]["isError"] is False

    resource = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 4,
            "method": "resources/read",
            "params": {"uri": "knowledge://spaces/space_1/assets/asset_1", "start": 0, "end": 4},
        },
    )
    assert resource.json()["result"]["contents"][0]["uri"].endswith("asset_1")


@pytest.mark.asyncio
async def test_mcp_sdk_session_reads_standard_resource_templates_and_fails_closed() -> None:
    app = _app()

    def httpx_client_factory(*, headers=None, timeout=None, auth=None):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://mcp.test",
            headers=headers,
            timeout=timeout,
            auth=auth,
        )

    client = MultiServerMCPClient(
        {
            "platform": {
                "transport": "streamable-http",
                "url": "http://mcp.test/mcp",
                "httpx_client_factory": httpx_client_factory,
            }
        }
    )
    async with client.session("platform") as session:
        resources = await session.list_resources()
        assert [str(item.uri) for item in resources.resources] == [
            "knowledge://spaces/space_1/manifest"
        ]
        templates = await session.list_resource_templates()
        assert templates.resourceTemplates[0].uriTemplate.endswith("/assets/{asset_id}")
        resource = await session.read_resource("knowledge://spaces/space_1/assets/asset_1")
        assert str(resource.contents[0].uri) == "knowledge://spaces/space_1/assets/asset_1"

    denied_app = FastAPI()
    denied_app.include_router(
        create_mcp_router(
            _Mcp(),
            principal_provider=lambda: Principal("mcp-test", ("knowledge:read",)),
            correlation_provider=lambda: Correlation("mcp-denied"),
        )
    )

    def denied_factory(*, headers=None, timeout=None, auth=None):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=denied_app),
            base_url="http://mcp.test",
            headers=headers,
            timeout=timeout,
            auth=auth,
        )

    denied_client = MultiServerMCPClient(
        {
            "platform": {
                "transport": "streamable-http",
                "url": "http://mcp.test/mcp",
                "httpx_client_factory": denied_factory,
            }
        }
    )
    async with denied_client.session("platform") as session:
        denied = await session.read_resource("knowledge://spaces/space_1/assets/asset_1")
        assert denied.contents == []
        assert denied.structuredContent["error"]["code"] == "permission_denied"
        unknown = await session.read_resource("knowledge://spaces/space_1/assets/missing")
        assert unknown.contents == []
        assert unknown.structuredContent["error"]["code"] == "not_found"


def test_mcp_json_rpc_edge_fails_closed_and_accepts_initialized_notification() -> None:
    client = TestClient(_app())

    invalid = client.post("/mcp", json={"jsonrpc": "1.0", "id": 1, "method": "ping"})
    assert invalid.json()["error"]["code"] == -32600
    unknown = client.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "nope"})
    assert unknown.json()["error"]["code"] == -32601
    missing_id = client.post("/mcp", json={"jsonrpc": "2.0", "method": "ping"})
    assert missing_id.json()["error"]["code"] == -32600
    boolean_id = client.post("/mcp", json={"jsonrpc": "2.0", "id": True, "method": "ping"})
    assert boolean_id.json()["error"]["code"] == -32600
    notification = client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert notification.status_code == 202

    non_object = client.post("/mcp", json=[])
    assert non_object.status_code == 200
    assert non_object.json()["error"]["code"] == -32600
    malformed = client.post("/mcp", content=b"{")
    assert malformed.status_code == 200
    assert malformed.json()["error"]["code"] == -32600
