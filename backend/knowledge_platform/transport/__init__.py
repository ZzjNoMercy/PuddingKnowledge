"""REST and MCP adapters for the Platform Query and Admin planes."""

from .admin_adapters import GbrainProjectionBinding, RestAdminAdapter, StaticProcessingBindingResolver
from .fastapi_admin_router import create_admin_router
from .fastapi_job_router import create_job_router
from .fastapi_router import create_query_router
from .job_adapters import RestJobAdapter
from .query_adapters import McpQueryAdapter, RestQueryAdapter

__all__ = [
    "McpQueryAdapter",
    "RestQueryAdapter",
    "RestAdminAdapter",
    "GbrainProjectionBinding",
    "StaticProcessingBindingResolver",
    "create_query_router",
    "create_admin_router",
    "create_job_router",
    "RestJobAdapter",
    "create_platform_app",
    "create_mcp_router",
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


def __getattr__(name: str):
    """Load the app composer lazily so baseline probes keep their imports stable."""

    if name == "create_platform_app":
        from .fastapi_app import create_platform_app

        return create_platform_app
    if name == "create_mcp_router":
        from .fastapi_mcp_router import create_mcp_router

        return create_mcp_router
    if name in {
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
    }:
        from . import external_mcp

        return getattr(external_mcp, name)
    raise AttributeError(name)
