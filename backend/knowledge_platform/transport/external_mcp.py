"""User-configured external Platform MCP discovery without local Tool injection."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from itertools import count
from urllib.parse import urlparse

from knowledge_contracts import CapabilityDescriptor, CapabilityInventory, Principal

PLATFORM_MCP_SERVER_NAME = "platform"
PLATFORM_MCP_TRANSPORT = "streamable-http"
PLATFORM_MCP_PROTOCOL_VERSION = "2025-06-18"
PLATFORM_MCP_REQUIRED_TOOLS = frozenset(
    {
        "knowledge_list",
        "knowledge_search",
        "knowledge_read",
        "document_rag_query",
        "wiki_query",
        "table_query",
        "knowledge_query",
        "database_nl2sql",
        "database_execute_readonly",
    }
)
_ENV_REF_RE = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*\}$")
_VAULT_REF_RE = re.compile(r"^vault://[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")


class ExternalPlatformMcpConfigError(ValueError):
    """External Platform MCP configuration is not safe to use."""


class ExternalPlatformMcpAuthError(ValueError):
    """External Platform MCP authentication could not be materialized safely."""


class ExternalPlatformMcpDiscoveryError(RuntimeError):
    """External Platform MCP capability discovery failed closed."""


class ExternalPlatformMcpInvocationError(RuntimeError):
    """External Platform MCP invocation failed at the protocol boundary."""


def _require_endpoint(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExternalPlatformMcpConfigError("Platform MCP endpoint is required")
    if value != value.strip() or any(ord(character) < 0x20 for character in value):
        raise ExternalPlatformMcpConfigError("Platform MCP endpoint contains invalid control data")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ExternalPlatformMcpConfigError("Platform MCP endpoint must use HTTP or HTTPS")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ExternalPlatformMcpConfigError("Platform MCP endpoint must not contain credentials or query data")
    return value


def _require_auth_ref(value: object) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not (_ENV_REF_RE.fullmatch(value) or _VAULT_REF_RE.fullmatch(value)):
        raise ExternalPlatformMcpConfigError("Platform MCP auth_ref must be an environment or Vault reference")
    return value


@dataclass(frozen=True, slots=True)
class ExternalPlatformMcpConfig:
    endpoint: str
    auth_ref: str | None = None
    protocol_version: str = PLATFORM_MCP_PROTOCOL_VERSION
    tool_allowlist: frozenset[str] = PLATFORM_MCP_REQUIRED_TOOLS

    def __post_init__(self) -> None:
        _require_endpoint(self.endpoint)
        if self.protocol_version != PLATFORM_MCP_PROTOCOL_VERSION:
            raise ExternalPlatformMcpConfigError("Platform MCP protocol version is unsupported")
        if not isinstance(self.tool_allowlist, frozenset) or not self.tool_allowlist:
            raise ExternalPlatformMcpConfigError("Platform MCP tool allowlist must not be empty")
        if not self.tool_allowlist.issubset(PLATFORM_MCP_REQUIRED_TOOLS):
            raise ExternalPlatformMcpConfigError("Platform MCP tool allowlist contains an unsupported tool")
        _require_auth_ref(self.auth_ref)

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> ExternalPlatformMcpConfig | None:
        if not isinstance(value, Mapping):
            raise ExternalPlatformMcpConfigError("Platform MCP config must be an object")
        allowed = {"enabled", "endpoint", "auth_ref", "protocol_version", "tool_allowlist"}
        if set(value) - allowed:
            raise ExternalPlatformMcpConfigError("Platform MCP config contains unsupported fields")
        enabled = value.get("enabled", False)
        if type(enabled) is not bool:
            raise ExternalPlatformMcpConfigError("Platform MCP enabled must be boolean")
        if not enabled:
            return None
        tool_allowlist = value.get("tool_allowlist", PLATFORM_MCP_REQUIRED_TOOLS)
        if not isinstance(tool_allowlist, (list, tuple, set, frozenset)) or any(
            not isinstance(tool, str) for tool in tool_allowlist
        ):
            raise ExternalPlatformMcpConfigError("Platform MCP tool allowlist is invalid")
        return cls(
            endpoint=_require_endpoint(value.get("endpoint")),
            auth_ref=_require_auth_ref(value.get("auth_ref")),
            protocol_version=value.get("protocol_version", PLATFORM_MCP_PROTOCOL_VERSION),
            tool_allowlist=frozenset(tool_allowlist),
        )

    def descriptor(self) -> dict[str, object]:
        """Return an external descriptor; never resolve or emit auth material."""

        return {
            "server_name": PLATFORM_MCP_SERVER_NAME,
            "transport": PLATFORM_MCP_TRANSPORT,
            "endpoint": self.endpoint,
            "auth_ref": self.auth_ref,
            "protocol_version": self.protocol_version,
            "tool_allowlist": sorted(self.tool_allowlist),
            "local_tool_registration": False,
        }


def discover_external_platform_mcp(config: Mapping[str, object]) -> ExternalPlatformMcpConfig | None:
    """Parse the user-owned Platform MCP section without contacting a server."""

    return ExternalPlatformMcpConfig.from_mapping(config)


def build_external_platform_mcp_client_config(
    config: ExternalPlatformMcpConfig,
    *,
    resolve_auth: Callable[[str], str] | None = None,
) -> dict[str, dict[str, object]]:
    """Build a generic MCP client config from a boundary-approved descriptor.

    Credential resolution is deliberately injected by the host.  This module
    never imports the legacy credential store, and the returned token is only
    held in the transient client configuration passed to the generic MCP
    client.
    """

    if not isinstance(config, ExternalPlatformMcpConfig):
        raise TypeError("Platform MCP client config requires a discovered config")
    headers: dict[str, object] = {}
    if config.auth_ref is not None:
        if resolve_auth is None:
            raise ExternalPlatformMcpAuthError("Platform MCP auth resolver is required")
        try:
            token = resolve_auth(config.auth_ref)
        except Exception as exc:
            raise ExternalPlatformMcpAuthError("Platform MCP auth resolution failed") from exc
        if (
            not isinstance(token, str)
            or not token
            or token != token.strip()
            or len(token) > 4096
            or any(character in token for character in "\r\n")
        ):
            raise ExternalPlatformMcpAuthError("Platform MCP auth value is invalid")
        headers["Authorization"] = f"Bearer {token}"
    return {
        PLATFORM_MCP_SERVER_NAME: {
            "transport": PLATFORM_MCP_TRANSPORT,
            "url": config.endpoint,
            "headers": headers,
        }
    }


ExternalPlatformMcpInventoryLoader = Callable[
    [Mapping[str, dict[str, object]], Principal], Awaitable[CapabilityInventory]
]


class ExternalPlatformMcpCapabilityDiscovery:
    """Adapt a generic external MCP inventory loader to the Agent port.

    The loader is the host's generic MCP client boundary.  This adapter does
    not inspect or register local business Tools; it only accepts a
    boundary-issued inventory and exposes authorized read capabilities to the
    generic Agent surface.
    """

    def __init__(
        self,
        config: ExternalPlatformMcpConfig,
        *,
        inventory_loader: ExternalPlatformMcpInventoryLoader,
        resolve_auth: Callable[[str], str] | None = None,
    ) -> None:
        if not isinstance(config, ExternalPlatformMcpConfig):
            raise TypeError("external Platform MCP discovery requires a discovered config")
        if not callable(inventory_loader):
            raise TypeError("external Platform MCP inventory loader must be callable")
        self._config = config
        self._inventory_loader = inventory_loader
        self._resolve_auth = resolve_auth

    async def discover(self, *, principal: Principal) -> CapabilityInventory:
        if not isinstance(principal, Principal):
            raise TypeError("external Platform MCP discovery requires a Principal")
        client_config = build_external_platform_mcp_client_config(
            self._config,
            resolve_auth=self._resolve_auth,
        )
        try:
            inventory = await self._inventory_loader(client_config, principal)
        except (ExternalPlatformMcpAuthError, ExternalPlatformMcpConfigError):
            raise
        except Exception as exc:
            raise ExternalPlatformMcpDiscoveryError("external Platform MCP inventory discovery failed") from exc
        if not isinstance(inventory, CapabilityInventory) or not inventory.issuer_id.strip():
            raise ExternalPlatformMcpDiscoveryError("external Platform MCP inventory is invalid")
        seen: set[str] = set()
        for capability in inventory.capabilities:
            if not isinstance(capability, CapabilityDescriptor):
                raise ExternalPlatformMcpDiscoveryError("external Platform MCP inventory contains an invalid capability")
            if capability.capability_id in seen:
                raise ExternalPlatformMcpDiscoveryError("external Platform MCP inventory contains duplicate capabilities")
            seen.add(capability.capability_id)
            if capability.authorized and not capability.read_only:
                raise ExternalPlatformMcpDiscoveryError(
                    "external Platform MCP inventory contains an authorized write capability"
                )
        return inventory


class ExternalPlatformMcpHttpClient:
    """Generic streamable-HTTP MCP client for the external Platform boundary.

    This adapter is intentionally independent of PuddingClaw's legacy MCP
    registry. It maps the remote protocol response into the framework-neutral
    capability contracts and never injects Agent/session business fields.
    """

    def __init__(
        self,
        config: ExternalPlatformMcpConfig,
        *,
        issuer_id: str,
        resolve_auth: Callable[[str], str] | None = None,
        http_transport: object | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        if not isinstance(config, ExternalPlatformMcpConfig):
            raise TypeError("external Platform MCP HTTP client requires a discovered config")
        if not isinstance(issuer_id, str) or not issuer_id.strip():
            raise ValueError("external Platform MCP issuer_id must not be empty")
        if not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool):
            raise ValueError("external Platform MCP timeout must be numeric")
        if timeout_seconds <= 0 or timeout_seconds > 60:
            raise ValueError("external Platform MCP timeout is out of range")
        self._config = config
        self._issuer_id = issuer_id
        self._resolve_auth = resolve_auth
        self._http_transport = http_transport
        self._timeout_seconds = float(timeout_seconds)
        self._request_ids = count(1)

    async def _rpc(
        self,
        *,
        method: str,
        params: Mapping[str, object] | None = None,
        notification: bool = False,
    ) -> dict[str, object] | None:
        try:
            import httpx
        except ImportError as exc:
            raise ExternalPlatformMcpDiscoveryError("httpx is required for external Platform MCP") from exc
        client_config = build_external_platform_mcp_client_config(
            self._config,
            resolve_auth=self._resolve_auth,
        )[PLATFORM_MCP_SERVER_NAME]
        headers = client_config.get("headers", {})
        if not isinstance(headers, Mapping):
            raise ExternalPlatformMcpInvocationError("external Platform MCP headers are invalid")
        request: dict[str, object] = {"jsonrpc": "2.0", "method": method}
        if not notification:
            request["id"] = next(self._request_ids)
        if params is not None:
            request["params"] = dict(params)
        try:
            async with httpx.AsyncClient(
                transport=self._http_transport,
                timeout=self._timeout_seconds,
            ) as client:
                response = await client.post(str(client_config["url"]), json=request, headers=dict(headers))
                if notification and response.status_code == 202:
                    return None
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ExternalPlatformMcpInvocationError("external Platform MCP HTTP exchange failed") from exc
        if not isinstance(payload, Mapping) or payload.get("jsonrpc") != "2.0" or "id" not in payload:
            raise ExternalPlatformMcpInvocationError("external Platform MCP response is invalid")
        if "error" in payload:
            raise ExternalPlatformMcpInvocationError("external Platform MCP returned a JSON-RPC error")
        result = payload.get("result")
        if not isinstance(result, Mapping):
            raise ExternalPlatformMcpInvocationError("external Platform MCP result is invalid")
        return dict(result)

    async def discover(self, *, principal: Principal) -> CapabilityInventory:
        if not isinstance(principal, Principal):
            raise TypeError("external Platform MCP discovery requires a Principal")
        initialized = await self._rpc(
            method="initialize",
            params={
                "protocolVersion": PLATFORM_MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "puddingclaw-harness", "version": "v1"},
            },
        )
        if initialized is None or initialized.get("protocolVersion") != PLATFORM_MCP_PROTOCOL_VERSION:
            raise ExternalPlatformMcpDiscoveryError("external Platform MCP protocol version mismatch")
        await self._rpc(method="notifications/initialized", notification=True)
        listed = await self._rpc(method="tools/list", params={})
        if listed is None or not isinstance(listed.get("tools"), list):
            raise ExternalPlatformMcpDiscoveryError("external Platform MCP tools/list result is invalid")
        capabilities: list[CapabilityDescriptor] = []
        for raw_tool in listed["tools"]:
            if not isinstance(raw_tool, Mapping):
                raise ExternalPlatformMcpDiscoveryError("external Platform MCP tool descriptor is invalid")
            name = raw_tool.get("name")
            if not isinstance(name, str) or not name.strip():
                raise ExternalPlatformMcpDiscoveryError("external Platform MCP tool name is invalid")
            if name not in self._config.tool_allowlist:
                continue
            capabilities.append(
                CapabilityDescriptor(
                    capability_id=f"{PLATFORM_MCP_SERVER_NAME}/{name}",
                    provider_id=PLATFORM_MCP_SERVER_NAME,
                    operation=name,
                    read_only=True,
                    authorized=True,
                    resource_schemes=("knowledge",),
                )
            )
        inventory = CapabilityInventory(tuple(capabilities), issuer_id=self._issuer_id)
        return inventory

    async def invoke(
        self,
        capability: CapabilityDescriptor,
        arguments: Mapping[str, object],
        *,
        principal: Principal,
        correlation,
    ) -> Mapping[str, object]:
        if not isinstance(capability, CapabilityDescriptor) or capability.provider_id != PLATFORM_MCP_SERVER_NAME:
            raise ExternalPlatformMcpInvocationError("capability does not belong to external Platform MCP")
        if capability.operation not in self._config.tool_allowlist:
            raise ExternalPlatformMcpInvocationError("capability is not in the external Platform MCP allowlist")
        if not isinstance(principal, Principal):
            raise TypeError("external Platform MCP invocation requires a Principal")
        params = {
            "name": capability.operation,
            "arguments": dict(arguments),
            "_meta": {"correlation_id": correlation.trace_id},
        }
        result = await self._rpc(method="tools/call", params=params)
        if result is None:
            raise ExternalPlatformMcpInvocationError("external Platform MCP invocation has no result")
        return result


def load_external_platform_mcp_config() -> ExternalPlatformMcpConfig | None:
    """Load only ``knowledge_platform.mcp`` from the host's user config."""

    try:
        from config import load_config

        section = load_config().get("knowledge_platform", {}).get("mcp", {})
    except Exception as exc:
        raise ExternalPlatformMcpConfigError("Platform MCP user config is unavailable") from exc
    return discover_external_platform_mcp(section)


__all__ = [
    "ExternalPlatformMcpConfig",
    "ExternalPlatformMcpConfigError",
    "ExternalPlatformMcpAuthError",
    "ExternalPlatformMcpDiscoveryError",
    "ExternalPlatformMcpInvocationError",
    "ExternalPlatformMcpInventoryLoader",
    "ExternalPlatformMcpCapabilityDiscovery",
    "ExternalPlatformMcpHttpClient",
    "PLATFORM_MCP_PROTOCOL_VERSION",
    "PLATFORM_MCP_REQUIRED_TOOLS",
    "PLATFORM_MCP_SERVER_NAME",
    "PLATFORM_MCP_TRANSPORT",
    "discover_external_platform_mcp",
    "build_external_platform_mcp_client_config",
    "load_external_platform_mcp_config",
]
