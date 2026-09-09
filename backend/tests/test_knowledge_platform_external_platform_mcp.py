from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from knowledge_contracts import CapabilityDescriptor, CapabilityInventory, Correlation, Principal
from knowledge_platform.transport.external_mcp import (
    PLATFORM_MCP_REQUIRED_TOOLS,
    ExternalPlatformMcpAuthError,
    ExternalPlatformMcpCapabilityDiscovery,
    ExternalPlatformMcpConfigError,
    ExternalPlatformMcpDiscoveryError,
    ExternalPlatformMcpHttpClient,
    ExternalPlatformMcpInvocationError,
    build_external_platform_mcp_client_config,
    discover_external_platform_mcp,
)


def test_external_platform_mcp_is_config_only_and_has_no_local_tool_registration() -> None:
    config = discover_external_platform_mcp(
        {
            "enabled": True,
            "endpoint": "https://platform.example.invalid/mcp",
            "auth_ref": "${PUDDINGKNOWLEDGE_MCP_TOKEN}",
        }
    )

    assert config is not None
    descriptor = config.descriptor()
    assert descriptor["server_name"] == "platform"
    assert descriptor["local_tool_registration"] is False
    assert set(descriptor["tool_allowlist"]) == PLATFORM_MCP_REQUIRED_TOOLS
    assert descriptor["auth_ref"] == "${PUDDINGKNOWLEDGE_MCP_TOKEN}"


def test_disabled_external_platform_mcp_does_not_create_a_binding() -> None:
    assert discover_external_platform_mcp({"enabled": False}) is None


def test_external_platform_mcp_client_config_uses_injected_auth_without_legacy_registry() -> None:
    config = discover_external_platform_mcp(
        {
            "enabled": True,
            "endpoint": "https://platform.example.invalid/mcp",
            "auth_ref": "vault://platform/mcp-token",
        }
    )
    assert config is not None
    seen: list[str] = []

    def resolve(reference: str) -> str:
        seen.append(reference)
        return "opaque-token"

    client_config = build_external_platform_mcp_client_config(config, resolve_auth=resolve)

    assert seen == ["vault://platform/mcp-token"]
    assert client_config == {
        "platform": {
            "transport": "streamable-http",
            "url": "https://platform.example.invalid/mcp",
            "headers": {"Authorization": "Bearer opaque-token"},
        }
    }


@pytest.mark.parametrize("token", ["", " token", "token ", "token\nvalue", "x" * 4097])
def test_external_platform_mcp_rejects_unsafe_resolved_auth(token: str) -> None:
    config = discover_external_platform_mcp(
        {
            "enabled": True,
            "endpoint": "https://platform.example.invalid/mcp",
            "auth_ref": "${PLATFORM_TOKEN}",
        }
    )
    assert config is not None
    with pytest.raises(ExternalPlatformMcpAuthError):
        build_external_platform_mcp_client_config(config, resolve_auth=lambda _ref: token)


def test_external_platform_mcp_requires_resolver_but_does_not_require_auth_when_unconfigured() -> None:
    with_auth = discover_external_platform_mcp(
        {
            "enabled": True,
            "endpoint": "https://platform.example.invalid/mcp",
            "auth_ref": "${PLATFORM_TOKEN}",
        }
    )
    assert with_auth is not None
    with pytest.raises(ExternalPlatformMcpAuthError):
        build_external_platform_mcp_client_config(with_auth)

    without_auth = discover_external_platform_mcp(
        {"enabled": True, "endpoint": "https://platform.example.invalid/mcp"}
    )
    assert without_auth is not None
    assert build_external_platform_mcp_client_config(without_auth)["platform"]["headers"] == {}


def test_external_platform_mcp_capability_discovery_uses_generic_loader_and_preserves_identity() -> None:
    config = discover_external_platform_mcp(
        {
            "enabled": True,
            "endpoint": "https://platform.example.invalid/mcp",
            "auth_ref": "${PLATFORM_TOKEN}",
        }
    )
    assert config is not None
    received: dict[str, object] = {}

    async def load_inventory(client_config, principal):
        received.update({"client_config": client_config, "principal": principal})
        return CapabilityInventory(
            (
                CapabilityDescriptor("platform/read", "platform", "read", True, True, ("knowledge",)),
                CapabilityDescriptor("platform/denied", "platform", "read", True, False, ("knowledge",)),
            ),
            issuer_id="platform-authz",
        )

    discovery = ExternalPlatformMcpCapabilityDiscovery(
        config,
        inventory_loader=load_inventory,
        resolve_auth=lambda reference: "opaque-token" if reference == "${PLATFORM_TOKEN}" else "",
    )
    principal = Principal("user-1")
    inventory = asyncio.run(discovery.discover(principal=principal))

    assert inventory.issuer_id == "platform-authz"
    assert received["principal"] == principal
    assert received["client_config"]["platform"]["headers"] == {"Authorization": "Bearer opaque-token"}


def test_external_platform_mcp_capability_discovery_rejects_authorized_write_and_duplicate_inventory() -> None:
    config = discover_external_platform_mcp(
        {"enabled": True, "endpoint": "https://platform.example.invalid/mcp"}
    )
    assert config is not None

    async def authorized_write(_client_config, _principal):
        return CapabilityInventory(
            (CapabilityDescriptor("platform/write", "platform", "write", False, True, ()),),
            issuer_id="platform-authz",
        )

    with pytest.raises(ExternalPlatformMcpDiscoveryError, match="write capability"):
        asyncio.run(
            ExternalPlatformMcpCapabilityDiscovery(config, inventory_loader=authorized_write).discover(
                principal=Principal("user-1")
            )
        )

    async def duplicate(_client_config, _principal):
        descriptor = CapabilityDescriptor("platform/read", "platform", "read", True, True, ("knowledge",))
        return CapabilityInventory((descriptor, descriptor), issuer_id="platform-authz")

    with pytest.raises(ExternalPlatformMcpDiscoveryError, match="duplicate"):
        asyncio.run(
            ExternalPlatformMcpCapabilityDiscovery(config, inventory_loader=duplicate).discover(
                principal=Principal("user-1")
            )
        )


def test_external_platform_mcp_capability_discovery_wraps_loader_failure() -> None:
    config = discover_external_platform_mcp(
        {"enabled": True, "endpoint": "https://platform.example.invalid/mcp"}
    )
    assert config is not None

    async def failing_loader(_client_config, _principal):
        raise ConnectionError("remote unavailable")

    with pytest.raises(ExternalPlatformMcpDiscoveryError, match="discovery failed"):
        asyncio.run(
            ExternalPlatformMcpCapabilityDiscovery(config, inventory_loader=failing_loader).discover(
                principal=Principal("user-1")
            )
        )


def test_external_platform_mcp_capability_discovery_rejects_malformed_inventory() -> None:
    config = discover_external_platform_mcp(
        {"enabled": True, "endpoint": "https://platform.example.invalid/mcp"}
    )
    assert config is not None

    async def malformed_loader(_client_config, _principal):
        return {"issuer_id": "platform-authz"}

    with pytest.raises(ExternalPlatformMcpDiscoveryError, match="inventory is invalid"):
        asyncio.run(
            ExternalPlatformMcpCapabilityDiscovery(config, inventory_loader=malformed_loader).discover(
                principal=Principal("user-1")
            )
        )


def test_external_platform_mcp_http_client_replays_real_json_rpc_sequence() -> None:
    calls: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        method = body["method"]
        if method == "initialize":
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": body["id"], "result": {"protocolVersion": "2025-06-18"}},
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/list":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {"tools": [{"name": "knowledge_query"}, {"name": "not_allowed"}]},
                },
            )
        if method == "tools/call":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {"structuredContent": {"status": "ok", "rows": 1}},
                },
            )
        return httpx.Response(404)

    config = discover_external_platform_mcp(
        {
            "enabled": True,
            "endpoint": "https://platform.example.invalid/mcp",
            "auth_ref": "${PLATFORM_TOKEN}",
            "tool_allowlist": ["knowledge_query"],
        }
    )
    assert config is not None
    client = ExternalPlatformMcpHttpClient(
        config,
        issuer_id="loopback-platform-authz",
        resolve_auth=lambda _reference: "opaque-token",
        http_transport=httpx.MockTransport(handler),
    )

    inventory = asyncio.run(client.discover(principal=Principal("user-1")))
    result = asyncio.run(
        client.invoke(
            inventory.capabilities[0],
            {"query": "PuddingClaw"},
            principal=Principal("user-1"),
            correlation=Correlation("correlation-1"),
        )
    )

    assert [capability.operation for capability in inventory.capabilities] == ["knowledge_query"]
    assert result == {"structuredContent": {"status": "ok", "rows": 1}}
    assert len(calls) == 4
    assert calls[-1]["params"]["_meta"] == {"correlation_id": "correlation-1"}
    assert all(call.get("method") != "notifications/initialized" or "id" not in call for call in calls)


def test_external_platform_mcp_http_client_rejects_protocol_and_json_rpc_failures() -> None:
    def protocol_mismatch(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": body.get("id"), "result": {"protocolVersion": "2024-11-05"}},
        )

    config = discover_external_platform_mcp(
        {"enabled": True, "endpoint": "https://platform.example.invalid/mcp"}
    )
    assert config is not None
    with pytest.raises(ExternalPlatformMcpDiscoveryError, match="version mismatch"):
        asyncio.run(
            ExternalPlatformMcpHttpClient(
                config,
                issuer_id="loopback-platform-authz",
                http_transport=httpx.MockTransport(protocol_mismatch),
            ).discover(principal=Principal("user-1"))
        )

    def rpc_error(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": body.get("id"), "error": {"code": -32000, "message": "denied"}},
        )

    with pytest.raises(ExternalPlatformMcpInvocationError, match="JSON-RPC error"):
        asyncio.run(
            ExternalPlatformMcpHttpClient(
                config,
                issuer_id="loopback-platform-authz",
                http_transport=httpx.MockTransport(rpc_error),
            )._rpc(method="ping")
        )


@pytest.mark.parametrize(
    "config, message",
    [
        ({"enabled": True, "endpoint": "file:///tmp/platform"}, "HTTP(S)"),
        ({"enabled": True, "endpoint": " https://platform.example.invalid/mcp"}, "control data"),
        ({"enabled": True, "endpoint": "https://platform.example.invalid/mcp\n"}, "control data"),
        ({"enabled": True, "endpoint": "https://user:secret@example.invalid/mcp"}, "credentials"),
        ({"enabled": True, "endpoint": "https://platform.example.invalid/mcp?token=secret"}, "query"),
        ({"enabled": True, "endpoint": "https://platform.example.invalid/mcp", "auth_ref": "Bearer secret"}, "auth_ref"),
        ({"enabled": True, "endpoint": "https://platform.example.invalid/mcp", "protocol_version": "2024-11-05"}, "version"),
        ({"enabled": True, "endpoint": "https://platform.example.invalid/mcp", "unknown": True}, "unsupported fields"),
    ],
)
def test_external_platform_mcp_rejects_unsafe_config(config: dict[str, object], message: str) -> None:
    with pytest.raises(ExternalPlatformMcpConfigError, match=message):
        discover_external_platform_mcp(config)


def test_external_platform_mcp_rejects_unknown_tools_and_invalid_enabled_type() -> None:
    with pytest.raises(ExternalPlatformMcpConfigError, match="unsupported tool"):
        discover_external_platform_mcp(
            {
                "enabled": True,
                "endpoint": "https://platform.example.invalid/mcp",
                "tool_allowlist": ["delete_everything"],
            }
        )
    with pytest.raises(ExternalPlatformMcpConfigError, match="boolean"):
        discover_external_platform_mcp(
            {"enabled": 1, "endpoint": "https://platform.example.invalid/mcp"}
        )
