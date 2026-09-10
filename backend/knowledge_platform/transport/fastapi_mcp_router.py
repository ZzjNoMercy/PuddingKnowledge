"""Small MCP JSON-RPC HTTP edge over the framework-neutral MCP adapter."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, Depends, Request, Response

from knowledge_contracts import Correlation, Principal

from .fastapi_router import CorrelationProvider, PrincipalProvider, _resolve
from .query_adapters import McpQueryAdapter


def _response(request_id: object, result: Mapping[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": dict(result)}


def _error(request_id: object, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _content(result: Mapping[str, Any]) -> list[dict[str, str]]:
    return [{"type": "text", "text": json.dumps(result, ensure_ascii=False, sort_keys=True)}]


def create_mcp_router(
    adapter: McpQueryAdapter,
    *,
    principal_provider: PrincipalProvider,
    correlation_provider: CorrelationProvider,
) -> APIRouter:
    """Expose MCP JSON-RPC methods without choosing auth or session storage."""

    router = APIRouter(prefix="/mcp", tags=["knowledge-mcp"])

    async def principal() -> Principal:
        return await _resolve(principal_provider())

    async def correlation() -> Correlation:
        return await _resolve(correlation_provider())

    @router.post("")
    async def mcp(
        request: Request,
        response: Response,
        current_principal: Principal = Depends(principal),
        current_correlation: Correlation = Depends(correlation),
    ):
        response.headers["Cache-Control"] = "no-store"
        try:
            body = await request.json()
        except (TypeError, ValueError):
            body = None
        if not isinstance(body, Mapping) or body.get("jsonrpc") != "2.0":
            return _error(body.get("id") if isinstance(body, Mapping) else None, -32600, "Invalid JSON-RPC request")
        request_id = body.get("id")
        method = body.get("method")
        params = body.get("params", {})
        if not isinstance(method, str) or not isinstance(params, Mapping):
            return _error(request_id, -32600, "Invalid JSON-RPC request")
        if method == "notifications/initialized":
            return Response(status_code=202)
        if "id" not in body or not isinstance(request_id, (str, int, float)) or isinstance(request_id, bool):
            return _error(None, -32600, "A non-notification JSON-RPC request requires a valid id")
        if method == "initialize":
            return _response(
                request_id,
                {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {"listChanged": False}, "resources": {"subscribe": False, "listChanged": False}},
                    "serverInfo": {"name": "knowledge-platform", "version": "v1"},
                },
            )
        if method == "ping":
            return _response(request_id, {})
        if method == "tools/list":
            return _response(request_id, {"tools": list(adapter.tool_descriptors())})
        if method == "resources/list":
            result = await adapter.list_resources(
                principal=current_principal,
                correlation=current_correlation,
            )
            return _response(request_id, result)
        if method == "resources/templates/list":
            return _response(
                request_id,
                {"resourceTemplates": list(adapter.resource_templates())},
            )
        if method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments", {})
            if not isinstance(name, str) or not isinstance(arguments, Mapping):
                return _error(request_id, -32602, "tools/call parameters are invalid")
            result = await adapter.call_tool(
                name=name,
                arguments=arguments,
                principal=current_principal,
                correlation=current_correlation,
            )
            structured = result.get("structuredContent", result)
            return _response(
                request_id,
                {
                    "content": _content(structured if isinstance(structured, Mapping) else {"result": structured}),
                    **result,
                    "isError": isinstance(structured, Mapping) and structured.get("status") == "error",
                },
            )
        if method == "resources/read":
            uri = params.get("uri")
            if not isinstance(uri, str):
                return _error(request_id, -32602, "resources/read uri is required")
            result = await adapter.read_resource(
                resource_uri=uri,
                principal=current_principal,
                correlation=current_correlation,
                start=params.get("start", 0),
                end=params.get("end", 8 * 1024 * 1024),
            )
            return _response(request_id, result)
        return _error(request_id, -32601, "Method not found")

    return router
