"""Bitable MCP surface tests over the real local Catalog/Vault fixture."""
import asyncio
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from knowledge_contracts import Correlation, Principal
from sqlalchemy.orm import Session

from knowledge_platform.catalog.models import KnowledgeConnector
from knowledge_platform.transport.bitable_adapters import BitableMcpQueryAdapter
from knowledge_platform.transport.fastapi_bitable_router import create_bitable_router
from knowledge_platform.transport.fastapi_mcp_router import create_mcp_router
from test_knowledge_platform_bitable_local import fixture  # noqa: F401 - register reusable pytest fixture


def _app(fixture, principal):
    service, api, engine, revision, *_ = fixture
    with Session(engine) as session, session.begin():
        session.add(KnowledgeConnector(
            id="wiki_source", space_id="space_kb_default", connector_key="feishu", name="Wiki",
            status="ready", auth_type="builtin", credential_ref="cred-wiki",
            config_json={"selection": {"kind": "wiki", "root": "", "wiki_space": ""}},
        ))
        session.add(KnowledgeConnector(
            id="bad_source", space_id="space_kb_default", connector_key="feishu", name="Broken",
            status="ready", auth_type="builtin", credential_ref="cred-bad", config_json={},
        ))
    service.config["wiki_source"] = {"id": "wiki_source"}
    service.config["bad_source"] = {"id": "bad_source"}
    adapter = BitableMcpQueryAdapter(object(), bitable=service.bitable)
    app = FastAPI()
    app.include_router(create_bitable_router(service.bitable, principal_provider=lambda: principal))
    app.include_router(create_mcp_router(
        adapter, principal_provider=lambda: principal,
        correlation_provider=lambda: Correlation("bitable-mcp-test"),
    ))
    return app, service, api, revision


def _rpc(client, method, request_id, params=None):
    response = client.post("/mcp", json={"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
    return response, response.json()


def test_bitable_mcp_initialize_tools_permissions_and_contract(fixture):
    principal = Principal("alice", scopes=("knowledge.query", "knowledge.space:space_kb_default"))
    app, service, api, revision = _app(fixture, principal)
    with TestClient(app) as client:
        initialized, init_body = _rpc(client, "initialize", 1, {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}})
        assert initialized.status_code == 200 and init_body["result"]["serverInfo"]["name"] == "knowledge-platform"
        listed, body = _rpc(client, "tools/list", 2)
        assert listed.headers["cache-control"] == "no-store"
        tools = {tool["name"]: tool for tool in body["result"]["tools"]}
        expected = {"feishu_bitable_list_sources", "feishu_bitable_describe", "feishu_bitable_relations", "feishu_bitable_query"}
        assert expected <= set(tools)
        assert tools["feishu_bitable_query"]["inputSchema"]["required"] == [
            "source_id", "table_id", "schema_revision", "field_names", "page_size", "cursor"
        ]
        assert "source_id" in tools["feishu_bitable_describe"]["inputSchema"]["required"]
        listed_sources, sources_body = _rpc(client, "tools/call", 3, {"name": "feishu_bitable_list_sources", "arguments": {}})
        assert listed_sources.headers["cache-control"] == "no-store"
        assert sources_body["result"]["structuredContent"]["status"] == "ok", sources_body
        assert [item["source_id"] for item in sources_body["result"]["structuredContent"]["data"]["sources"]] == ["source_1"]
        described, describe_body = _rpc(client, "tools/call", 4, {"name": "feishu_bitable_describe", "arguments": {"source_id": "source_1", "table_id": "tbl_1"}})
        assert described.status_code == 200 and describe_body["result"]["structuredContent"]["status"] == "ok"
        assert describe_body["result"]["structuredContent"]["data"]["schema_revision"] == revision

        query_args = {"source_id": "source_1", "table_id": "tbl_1", "schema_revision": revision,
                      "field_names": ["Name"], "page_size": 1, "cursor": ""}
        rest = client.post("/v1/sources/source_1/bitable/query", json={k: v for k, v in query_args.items() if k != "source_id"})
        mcp, mcp_body = _rpc(client, "tools/call", 5, {"name": "feishu_bitable_query", "arguments": query_args})
        assert rest.headers["cache-control"] == "no-store" and mcp.headers["cache-control"] == "no-store"
        rest_data = rest.json()["data"]
        mcp_data = mcp_body["result"]["structuredContent"]["data"]
        assert {key: value for key, value in rest_data.items() if key != "next_cursor"} == {
            key: value for key, value in mcp_data.items() if key != "next_cursor"
        }
        rest_cursor=json.loads(service.vault.vault.decrypt(rest_data["next_cursor"].encode(), context="bitable-cursor"))
        mcp_cursor=json.loads(service.vault.vault.decrypt(mcp_data["next_cursor"].encode(), context="bitable-cursor"))
        assert {k:v for k,v in rest_cursor.items() if k!='expires'}=={k:v for k,v in mcp_cursor.items() if k!='expires'}
        assert abs(rest_cursor['expires']-mcp_cursor['expires']) <= 5
        assert mcp_body["result"]["isError"] is False
        assert mcp_body["result"]["structuredContent"]["data"]["row_storage"] is False
        assert api.calls and api.calls[-1]["page_token"] == ""

        forged = dict(query_args, principal_id="bob")
        forged_response, forged_body = _rpc(client, "tools/call", 6, {"name": "feishu_bitable_query", "arguments": forged})
        assert forged_response.headers["cache-control"] == "no-store"
        assert forged_body["result"]["isError"] is True
        assert forged_body["result"]["structuredContent"]["error"]["code"] == "invalid_request"


def test_bitable_mcp_tenant_principal_is_denied_and_unregistered_source_is_not_advertised(fixture):
    principal = Principal("tenant-user", scopes=("knowledge.query", "knowledge.space:space_kb_default"), tenant_id="tenant-1")
    app, _, _, _ = _app(fixture, principal)
    with TestClient(app) as client:
        _, body = _rpc(client, "tools/call", 1, {"name": "feishu_bitable_list_sources", "arguments": {}})
        structured = body["result"]["structuredContent"]
        assert body["result"]["isError"] is True
        assert structured["error"]["code"] == "permission_denied"


@pytest.mark.asyncio
async def test_generic_langchain_mcp_consumer_discovers_prefixed_bitable_tools(fixture):
    from langchain_mcp_adapters.client import MultiServerMCPClient
    import httpx

    principal = Principal("alice", scopes=("knowledge.query", "knowledge.space:space_kb_default"))
    app, _, _, revision = _app(fixture, principal)

    def factory(*, headers=None, timeout=None, auth=None):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://bitable-mcp.test",
                                  headers=headers, timeout=timeout, auth=auth)

    client = MultiServerMCPClient({"platform": {
        "transport": "streamable-http", "url": "http://bitable-mcp.test/mcp",
        "httpx_client_factory": factory,
    }}, tool_name_prefix=True)
    tools = await client.get_tools(server_name="platform")
    names = {tool.name for tool in tools}
    assert "platform_feishu_bitable_query" in names
    query = next(tool for tool in tools if tool.name == "platform_feishu_bitable_query")
    result = await query.ainvoke({"source_id": "source_1", "table_id": "tbl_1", "schema_revision": revision,
                                  "field_names": ["Name"], "page_size": 1, "cursor": ""})
    assert "ROW_CANARY_47021" in str(result)
