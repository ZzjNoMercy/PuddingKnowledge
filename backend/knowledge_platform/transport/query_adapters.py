"""Small REST/MCP-shaped adapters over Platform application services.

These adapters deliberately do not import FastAPI or an MCP SDK.  A concrete
HTTP or MCP server can map its request/response objects to these JSON-safe
methods without duplicating authorization or query semantics.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from knowledge_contracts import Correlation, Principal, QueryErrorCode, QueryResult, is_valid_knowledge_uri
from knowledge_contracts.artifacts import MAX_BLOB_READ_BYTES
from knowledge_platform.catalog.deployment import (
    DeploymentActivationController,
    DeploymentActivationError,
    DeploymentReadContext,
)
from knowledge_platform.catalog.service import CatalogQueryService
from knowledge_platform.database.schema_service import DatabaseSchemaQueryService
from knowledge_platform.database.services import DatabaseExecuteReadonlyService, DatabaseNl2SqlService
from knowledge_platform.retrieval.services import (
    AssetReadService,
    CatalogSearchService,
    DocumentRetrievalService,
    QueryResultArtifactReadService,
    WikiQueryService,
)
from knowledge_platform.router import KnowledgeQueryRequest, KnowledgeQueryRouter
from knowledge_platform.structured.services import TableQueryService

_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
_ASSET_PATH_RE = re.compile(r"^/v1/assets/([A-Za-z0-9._:-]{1,160})$")
_ASSET_READ_PATH_RE = re.compile(r"^/v1/assets/([A-Za-z0-9._:-]{1,160}):read$")
_ASSET_DERIVATIVES_PATH_RE = re.compile(r"^/v1/assets/([A-Za-z0-9._:-]{1,160})/derivatives$")
_ASSET_DERIVATIVE_READ_PATH_RE = re.compile(
    r"^/v1/assets/([A-Za-z0-9._:-]{1,160})/derivatives/([A-Za-z0-9._:-]{1,160})$"
)
_DATABASE_EXECUTE_PATH_RE = re.compile(r"^/v1/database/query-plans/([A-Za-z0-9._:-]{1,160}):execute$")
_QUERY_RESULT_PATH_RE = re.compile(r"^/v1/query-results/([A-Za-z0-9._:-]{1,160})$")
_QUERY_RESULT_ARTIFACT_URI_RE = re.compile(
    r"^knowledge://query-results/([A-Za-z0-9._:-]{1,160})/artifact$"
)
_SPACE_MANIFEST_URI_RE = re.compile(r"^knowledge://spaces/([A-Za-z0-9._:-]{1,160})/manifest$")
_COLLECTION_RESOURCE_URI_RE = re.compile(
    r"^knowledge://spaces/([A-Za-z0-9._:-]{1,160})/collections/([A-Za-z0-9._:-]{1,160})$"
)
_LEGACY_DATASET_RESOURCE_URI_RE = re.compile(
    r"^knowledge://spaces/([A-Za-z0-9._:-]{1,160})/datasets/([A-Za-z0-9._:-]{1,160})$"
)
_DATABASE_SCHEMA_RESOURCE_URI_RE = re.compile(
    r"^knowledge://spaces/([A-Za-z0-9._:-]{1,160})/databases/([A-Za-z0-9._:-]{1,160})/schema/([A-Za-z0-9._:-]{1,160})$"
)
_NON_PORTABLE_MCP_TEXT_RE = re.compile(
    r"(?:file://|(?:^|[^A-Za-z0-9_])[A-Za-z]:[\\/]|\\\\|(?:^|[\s(=])/(?:[^\s]+)|(?:^|[\s(])~/)",
    re.IGNORECASE,
)


def _mcp_text(value: object, fallback: str) -> str:
    """Keep Catalog labels/descriptions portable at the MCP boundary."""

    if not isinstance(value, str) or not value.strip():
        return fallback
    if any(ord(character) < 32 and character not in "\t\n\r" for character in value):
        return fallback
    if _NON_PORTABLE_MCP_TEXT_RE.search(value):
        return fallback
    return value


def _error(
    correlation: Correlation,
    code: QueryErrorCode,
    message: str,
    *,
    retryable: bool = False,
) -> QueryResult:
    from knowledge_contracts import QueryError

    return QueryResult(
        status="error",
        trace_id=correlation.trace_id,
        error=QueryError(code=code, message=message, retryable=retryable),
    )


class RestQueryAdapter:
    """Route only the Phase 3 Query Plane endpoints to shared services."""

    def __init__(
        self,
        *,
        catalog: CatalogQueryService,
        search: CatalogSearchService,
        asset_read: AssetReadService,
        document: DocumentRetrievalService,
        wiki: WikiQueryService,
        table: TableQueryService | None = None,
        database_nl2sql: DatabaseNl2SqlService | None = None,
        database_execute: DatabaseExecuteReadonlyService | None = None,
        database_schema: DatabaseSchemaQueryService | None = None,
        derivatives: Any | None = None,
        knowledge_query: KnowledgeQueryRouter | None = None,
        deployment: DeploymentActivationController | None = None,
    ) -> None:
        self._catalog = catalog
        self._search = search
        self._asset_read = asset_read
        self._document = document
        self._wiki = wiki
        self._table = table
        self._database_nl2sql = database_nl2sql
        self._database_execute = database_execute
        self._database_schema = database_schema
        self._derivatives = derivatives
        self._knowledge_query = knowledge_query
        self._deployment = deployment

    @property
    def database_schema_available(self) -> bool:
        """Whether this adapter has an authorized database schema service."""

        return self._database_schema is not None

    async def handle(
        self,
        *,
        method: str,
        path: str,
        principal: Principal,
        correlation: Correlation,
        body: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        read_context: DeploymentReadContext | None = None
        if self._deployment is not None:
            try:
                read_context = self._deployment.capture_read_context()
            except DeploymentActivationError:
                return _error(
                    correlation,
                    QueryErrorCode.CAPABILITY_UNAVAILABLE,
                    "deployment revision is unavailable",
                ).to_dict()
        result = await self._handle_unfenced(
            method=method,
            path=path,
            principal=principal,
            correlation=correlation,
            body=body,
        )
        if self._deployment is not None and read_context is not None:
            try:
                self._deployment.assert_read_context(read_context)
            except DeploymentActivationError:
                return _error(
                    correlation,
                    QueryErrorCode.STALE_DEPLOYMENT_REVISION,
                    "deployment revision changed; retry the read",
                    retryable=True,
                ).to_dict()
        return result

    async def _handle_unfenced(
        self,
        *,
        method: str,
        path: str,
        principal: Principal,
        correlation: Correlation,
        body: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if body is None:
            body = {}
        if not isinstance(body, Mapping):
            return _error(correlation, QueryErrorCode.INVALID_REQUEST, "request body must be an object").to_dict()
        method = method.upper()
        if method == "GET" and path == "/v1/spaces":
            return self._catalog.list_spaces(principal=principal, correlation=correlation).to_dict()
        if method == "GET" and path in {"/v1/collections", "/v1/datasets"}:
            return self._catalog.list_collections(
                principal=principal, correlation=correlation, space_id=body.get("space_id")
            ).to_dict()
        if method == "GET" and path == "/v1/assets":
            return self._catalog.list_assets(
                principal=principal, correlation=correlation, space_id=body.get("space_id")
            ).to_dict()
        asset_match = _ASSET_PATH_RE.fullmatch(path)
        if method == "GET" and asset_match:
            return self._catalog.read_asset(
                principal=principal, correlation=correlation, asset_id=asset_match.group(1)
            ).to_dict()
        derivatives_match = _ASSET_DERIVATIVES_PATH_RE.fullmatch(path)
        if method == "GET" and derivatives_match:
            if self._derivatives is None:
                return _error(correlation, QueryErrorCode.CAPABILITY_UNAVAILABLE, "Asset derivatives are unavailable").to_dict()
            return self._derivatives.list(
                principal=principal, correlation=correlation, asset_id=derivatives_match.group(1)
            ).to_dict()
        derivative_read_match = _ASSET_DERIVATIVE_READ_PATH_RE.fullmatch(path)
        if method == "GET" and derivative_read_match:
            if self._derivatives is None:
                return _error(correlation, QueryErrorCode.CAPABILITY_UNAVAILABLE, "Asset derivatives are unavailable").to_dict()
            return (
                await self._derivatives.read(
                    principal=principal,
                    correlation=correlation,
                    asset_id=derivative_read_match.group(1),
                    kind=derivative_read_match.group(2),
                    start=body.get("start", 0),
                    end=body.get("end"),
                    expected_digest=body.get("expected_digest"),
                )
            ).to_dict()
        query_result_match = _QUERY_RESULT_PATH_RE.fullmatch(path)
        if method == "GET" and query_result_match:
            return self._catalog.read_query_result(
                principal=principal, correlation=correlation, query_result_id=query_result_match.group(1)
            ).to_dict()
        if method == "POST" and path == "/v1/search":
            return self._search.search_portal(
                principal=principal,
                correlation=correlation,
                text=body.get("query", body.get("text", "")),
                space_id=body.get("space_id"),
                limit=body.get("limit", 20),
            ).to_dict()
        asset_read_match = _ASSET_READ_PATH_RE.fullmatch(path)
        if method == "POST" and asset_read_match:
            metadata = self._catalog.read_asset(
                principal=principal, correlation=correlation, asset_id=asset_read_match.group(1)
            )
            if metadata.status == "error":
                return metadata.to_dict()
            asset = metadata.data.get("asset")
            if not isinstance(asset, Mapping):
                return _error(correlation, QueryErrorCode.INTERNAL_ERROR, "Asset metadata is malformed").to_dict()
            return (
                await self._asset_read.read(
                    principal=principal,
                    correlation=correlation,
                    resource_uri=str(body.get("resource_uri") or asset.get("source_uri") or ""),
                    start=body.get("start", 0),
                    end=body.get("end"),
                    expected_digest=body.get("expected_digest"),
                )
            ).to_dict()
        if method == "POST" and path in {"/v1/document-rag/query", "/v1/wiki/query"}:
            service = self._document if path.endswith("document-rag/query") else self._wiki
            return (
                await service.query(
                    principal=principal,
                    correlation=correlation,
                    query=body.get("query", ""),
                    space_id=body.get("space_id"),
                    limit=body.get("limit", 20),
                )
            ).to_dict()
        if method == "POST" and path == "/v1/table/query":
            if self._table is None:
                return _error(correlation, QueryErrorCode.CAPABILITY_UNAVAILABLE, "table query is unavailable").to_dict()
            return (
                await self._table.query(
                    principal=principal,
                    correlation=correlation,
                    query=body.get("query", ""),
                    asset_id=body.get("asset_id"),
                    dataset_id=body.get("dataset_id"),
                    space_id=body.get("space_id"),
                    limit=body.get("limit", 5),
                    semantic_context=body.get("semantic_context"),
                )
            ).to_dict()
        if method == "POST" and path == "/v1/database/nl2sql":
            if self._database_nl2sql is None:
                return _error(correlation, QueryErrorCode.CAPABILITY_UNAVAILABLE, "database_nl2sql is unavailable").to_dict()
            semantic_asset_ids = body.get("semantic_asset_ids", [])
            if not isinstance(semantic_asset_ids, (list, tuple)):
                return _error(correlation, QueryErrorCode.INVALID_REQUEST, "semantic_asset_ids must be an array").to_dict()
            return self._database_nl2sql.generate(
                principal=principal,
                correlation=correlation,
                space_id=body.get("space_id", ""),
                dataset_id=body.get("dataset_id", ""),
                question=body.get("question", body.get("query", "")),
                semantic_asset_ids=tuple(semantic_asset_ids),
            ).to_dict()
        if method == "GET" and path == "/v1/database/schema":
            if self._database_schema is None:
                return _error(correlation, QueryErrorCode.CAPABILITY_UNAVAILABLE, "database schema is unavailable").to_dict()
            return self._database_schema.list(
                principal=principal,
                correlation=correlation,
                space_id=body.get("space_id", ""),
                dataset_id=body.get("dataset_id", ""),
            ).to_dict()
        if method == "POST" and path in {"/v1/query", "/v1/knowledge/query"}:
            if self._knowledge_query is None:
                return _error(correlation, QueryErrorCode.CAPABILITY_UNAVAILABLE, "knowledge_query is unavailable").to_dict()
            try:
                request = KnowledgeQueryRequest(
                    query=body.get("query", ""),
                    space_id=body.get("space_id"),
                    collection_id=body.get("collection_id"),
                    capability_hint=body.get("capability_hint"),
                    max_age_seconds=body.get("max_age_seconds"),
                    max_cost_units=body.get("max_cost_units", 10),
                    limit=body.get("limit", 10),
                )
            except (TypeError, ValueError):
                return _error(correlation, QueryErrorCode.INVALID_REQUEST, "knowledge_query request is invalid").to_dict()
            return (
                await self._knowledge_query.query(
                    principal=principal,
                    correlation=correlation,
                    request=request,
                )
            ).to_dict()
        execute_match = _DATABASE_EXECUTE_PATH_RE.fullmatch(path)
        if method == "POST" and execute_match:
            if self._database_execute is None:
                return _error(correlation, QueryErrorCode.CAPABILITY_UNAVAILABLE, "database execution is unavailable").to_dict()
            return self._database_execute.execute(
                principal=principal,
                correlation=correlation,
                space_id=body.get("space_id", ""),
                query_plan_id=execute_match.group(1),
                page_size=body.get("page_size", 100),
                expected_sql_hash=body.get("expected_sql_hash"),
            ).to_dict()
        return _error(correlation, QueryErrorCode.NOT_FOUND, "Query endpoint was not found").to_dict()


class McpQueryAdapter:
    """MCP-shaped Tool/Resource mapping over the same REST application services."""

    def __init__(
        self,
        rest: RestQueryAdapter,
        query_result_artifact: QueryResultArtifactReadService | None = None,
        semantic_markdown: Any | None = None,
    ) -> None:
        self._rest = rest
        self._query_result_artifact = query_result_artifact
        self._semantic_markdown = semantic_markdown

    def resource_templates(self) -> tuple[dict[str, Any], ...]:
        """Return only templates backed by a working ``resources/read`` path."""

        templates = [
            {"uriTemplate": "knowledge://spaces/{space_id}/manifest", "name": "Space manifest", "mimeType": "application/json"},
            {"uriTemplate": "knowledge://spaces/{space_id}/collections/{collection_id}", "name": "Collection manifest", "mimeType": "application/json"},
            {"uriTemplate": "knowledge://spaces/{space_id}/assets/{asset_id}", "name": "Asset", "mimeType": "application/octet-stream"},
            {"uriTemplate": "knowledge://spaces/{space_id}/assets/{asset_id}/derivatives/{kind}", "name": "Asset derivative", "mimeType": "application/octet-stream"},
        ]
        if self._query_result_artifact is not None and self._query_result_artifact.resource_template_available:
            templates.append(
                {"uriTemplate": "knowledge://query-results/{query_result_id}/artifact", "name": "QueryResult artifact", "mimeType": "application/octet-stream"}
            )
        if self._semantic_markdown is not None:
            templates.append(
                {"uriTemplate": "knowledge://spaces/{space_id}/semantics/{semantic_asset_id}", "name": "Semantic Markdown", "mimeType": "text/markdown"}
            )
        if getattr(self._rest, "database_schema_available", False):
            templates.append(
                {
                    "uriTemplate": "knowledge://spaces/{space_id}/databases/{dataset_id}/schema/{table_name}",
                    "name": "Database schema",
                    "mimeType": "application/json",
                }
            )
        return tuple(templates)

    @staticmethod
    def _space_visible(principal: Principal, space_id: str, *, operation: str = "list") -> bool:
        scopes = set(principal.scopes)
        if principal.tenant_id is not None:
            return False
        if {"knowledge.admin", "knowledge:admin"} & scopes:
            return True
        required = {"knowledge.list", "knowledge:list"} if operation == "list" else {"knowledge.read", "knowledge:read"}
        if not required & scopes:
            return False
        scoped_spaces = {
            scope.split(":", 1)[1]
            for scope in scopes
            if scope.startswith("knowledge.space:")
        } | {
            scope.split(":", 2)[2]
            for scope in scopes
            if scope.startswith("knowledge:space:")
        }
        return not scoped_spaces or space_id in scoped_spaces

    async def list_resources(
        self,
        *,
        principal: Principal,
        correlation: Correlation,
    ) -> dict[str, Any]:
        """List only curated Space/Collection entries and fixed templates."""

        spaces_result = await self._rest.handle(
            method="GET", path="/v1/spaces", principal=principal, correlation=correlation
        )
        if spaces_result.get("status") != "ok":
            return {
                "resources": [],
                "resourceTemplates": list(self.resource_templates()),
                "structuredContent": spaces_result,
                "isError": True,
            }
        datasets_result = await self._rest.handle(
            method="GET", path="/v1/collections", principal=principal, correlation=correlation
        )
        if datasets_result.get("status") != "ok":
            return {
                "resources": [],
                "resourceTemplates": list(self.resource_templates()),
                "structuredContent": datasets_result,
                "isError": True,
            }
        resources: list[dict[str, Any]] = []
        for space in spaces_result.get("data", {}).get("spaces", []):
            if not isinstance(space, Mapping) or not _ID_RE.fullmatch(str(space.get("id") or "")):
                continue
            space_id = str(space["id"])
            if not self._space_visible(principal, space_id, operation="list"):
                continue
            resources.append(
                {
                    "uri": f"knowledge://spaces/{space_id}/manifest",
                    "name": _mcp_text(space.get("name"), space_id),
                    "description": _mcp_text(space.get("description"), "Local Knowledge Space exposed through the Platform."),
                    "mimeType": "application/json",
                }
            )
        datasets = datasets_result.get("data", {}).get("datasets")
        if not isinstance(datasets, list):
            datasets = datasets_result.get("data", {}).get("collections", [])
        for dataset in datasets:
            if not isinstance(dataset, Mapping):
                continue
            space_id = str(dataset.get("space_id") or "")
            dataset_id = str(dataset.get("id") or "")
            if (
                not _ID_RE.fullmatch(space_id)
                or not _ID_RE.fullmatch(dataset_id)
                or not self._space_visible(principal, space_id)
            ):
                continue
            resources.append(
                {
                    "uri": f"knowledge://spaces/{space_id}/collections/{dataset_id}",
                    "name": _mcp_text(dataset.get("name"), dataset_id),
                    "description": "Platform Collection manifest",
                    "mimeType": "application/json",
                }
            )
        return {"resources": resources, "resourceTemplates": list(self.resource_templates())}

    @staticmethod
    def tool_descriptors() -> tuple[dict[str, Any], ...]:
        return (
            {"name": "knowledge_list", "inputSchema": {"type": "object", "properties": {"space_id": {"type": "string"}}}},
            {"name": "knowledge_search", "inputSchema": {"type": "object", "required": ["query"]}},
            {"name": "knowledge_read", "inputSchema": {"type": "object", "required": ["asset_id", "end"]}},
            {"name": "document_rag_query", "inputSchema": {"type": "object", "required": ["query"]}},
            {"name": "wiki_query", "inputSchema": {"type": "object", "required": ["query"]}},
            {
                "name": "table_query",
                "inputSchema": {
                    "type": "object",
                    "required": ["query"],
                    "properties": {
                        "query": {"type": "string"},
                        "asset_id": {"type": "string"},
                        "dataset_id": {"type": "string"},
                        "space_id": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                        "semantic_context": {"type": "object"},
                    },
                },
            },
            {
                "name": "knowledge_query",
                "inputSchema": {
                    "type": "object",
                    "required": ["query"],
                    "properties": {
                        "query": {"type": "string"},
                        "space_id": {"type": "string"},
                        "collection_id": {"type": "string"},
                        "capability_hint": {
                            "type": "string",
                            "enum": ["document_rag_query", "wiki_query", "table_query", "database_nl2sql"],
                        },
                        "max_age_seconds": {"type": "integer", "minimum": 0, "maximum": 604800},
                        "max_cost_units": {"type": "integer", "minimum": 1, "maximum": 100},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                    },
                },
            },
            {
                "name": "database_nl2sql",
                "inputSchema": {
                    "type": "object",
                    "required": ["space_id", "dataset_id", "question"],
                    "properties": {
                        "space_id": {"type": "string"},
                        "dataset_id": {"type": "string"},
                        "question": {"type": "string"},
                        "semantic_asset_ids": {"type": "array", "items": {"type": "string"}},
                    },
                },
            },
            {
                "name": "database_execute_readonly",
                "inputSchema": {
                    "type": "object",
                    "required": ["space_id", "query_plan_id"],
                    "properties": {
                        "space_id": {"type": "string"},
                        "query_plan_id": {"type": "string"},
                        "page_size": {"type": "integer", "minimum": 1, "maximum": 500},
                        "expected_sql_hash": {"type": "string"},
                    },
                },
            },
        )

    async def call_tool(
        self,
        *,
        name: str,
        arguments: Mapping[str, Any] | None,
        principal: Principal,
        correlation: Correlation,
    ) -> dict[str, Any]:
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, Mapping):
            return {"structuredContent": _error(correlation, QueryErrorCode.INVALID_REQUEST, "Tool arguments must be an object").to_dict()}
        if name == "knowledge_list":
            result = await self._rest.handle(
                method="GET", path="/v1/collections", principal=principal, correlation=correlation, body=arguments
            )
        elif name == "knowledge_search":
            result = await self._rest.handle(
                method="POST", path="/v1/search", principal=principal, correlation=correlation, body=arguments
            )
        elif name == "knowledge_read":
            asset_id = arguments.get("asset_id")
            if not isinstance(asset_id, str) or not _ID_RE.fullmatch(asset_id):
                return _error(correlation, QueryErrorCode.INVALID_REQUEST, "asset_id is invalid").to_dict()
            result = await self._rest.handle(
                method="POST",
                path=f"/v1/assets/{asset_id}:read",
                principal=principal,
                correlation=correlation,
                body=arguments,
            )
        elif name in {"document_rag_query", "wiki_query", "table_query", "knowledge_query", "database_nl2sql", "database_execute_readonly"}:
            if name == "table_query":
                result = await self._rest.handle(
                    method="POST", path="/v1/table/query", principal=principal, correlation=correlation, body=arguments
                )
                return {"structuredContent": result}
            if name == "database_nl2sql":
                result = await self._rest.handle(
                    method="POST", path="/v1/database/nl2sql", principal=principal, correlation=correlation, body=arguments
                )
                return {"structuredContent": result}
            if name == "database_execute_readonly":
                query_plan_id = arguments.get("query_plan_id")
                if not isinstance(query_plan_id, str) or not _ID_RE.fullmatch(query_plan_id):
                    return _error(correlation, QueryErrorCode.INVALID_REQUEST, "query_plan_id is invalid").to_dict()
                result = await self._rest.handle(
                    method="POST",
                    path=f"/v1/database/query-plans/{query_plan_id}:execute",
                    principal=principal,
                    correlation=correlation,
                    body=arguments,
                )
                return {"structuredContent": result}
            if name == "knowledge_query":
                result = await self._rest.handle(
                    method="POST", path="/v1/knowledge/query", principal=principal, correlation=correlation, body=arguments
                )
                return {"structuredContent": result}
            result = await self._rest.handle(
                method="POST",
                path="/v1/document-rag/query" if name == "document_rag_query" else "/v1/wiki/query",
                principal=principal,
                correlation=correlation,
                body=arguments,
            )
        else:
            result = _error(correlation, QueryErrorCode.NOT_FOUND, "MCP Tool was not found").to_dict()
        return {"structuredContent": result}

    async def read_resource(
        self,
        *,
        resource_uri: str,
        principal: Principal,
        correlation: Correlation,
        start: int = 0,
        end: int = MAX_BLOB_READ_BYTES,
    ) -> dict[str, Any]:
        semantic_parts = resource_uri.removeprefix("knowledge://").split("/")
        is_semantic_resource = (
            len(semantic_parts) == 4
            and semantic_parts[0] == "spaces"
            and semantic_parts[2] == "semantics"
        )
        if is_semantic_resource:
            if self._semantic_markdown is None:
                return {"contents": [], "structuredContent": _error(correlation, QueryErrorCode.BINDING_UNAVAILABLE, "Semantic Markdown registry is unavailable").to_dict()}
            result = self._semantic_markdown.read_resource(
                principal=principal,
                correlation=correlation,
                resource_uri=resource_uri,
                start=start,
                end=end,
            )
            if result.status != "ok":
                return {"contents": [], "structuredContent": result.to_dict()}
            data = result.data
            return {
                "contents": [{
                    "uri": resource_uri,
                    "mimeType": "text/markdown",
                    "text": data.get("content_text", ""),
                }],
                "structuredContent": result.to_dict(),
            }
        query_result_artifact = _QUERY_RESULT_ARTIFACT_URI_RE.fullmatch(resource_uri)
        if query_result_artifact:
            if self._query_result_artifact is None:
                return {"contents": [], "structuredContent": _error(correlation, QueryErrorCode.BINDING_UNAVAILABLE, "QueryResult artifact is unavailable").to_dict()}
            result = await self._query_result_artifact.read(
                query_result_id=query_result_artifact.group(1),
                resource_uri=resource_uri,
                principal=principal,
                correlation=correlation,
                start=start,
                end=end,
            )
            if result.status != "ok":
                return {"contents": [], "structuredContent": result.to_dict()}
            data = result.data
            return {
                "contents": [
                    {
                        "uri": resource_uri,
                        "mimeType": "application/octet-stream",
                        "blob": data.get("content_base64", ""),
                    }
                ],
                "structuredContent": result.to_dict(),
            }
        database_schema_resource = _DATABASE_SCHEMA_RESOURCE_URI_RE.fullmatch(resource_uri)
        if database_schema_resource:
            if not getattr(self._rest, "database_schema_available", False):
                unavailable = _error(
                    correlation,
                    QueryErrorCode.BINDING_UNAVAILABLE,
                    "Database schema is unavailable",
                ).to_dict()
                return {"contents": [], "structuredContent": unavailable}
            space_id, dataset_id, table_name = database_schema_resource.groups()
            result = await self._rest.handle(
                method="GET",
                path="/v1/database/schema",
                principal=principal,
                correlation=correlation,
                body={"space_id": space_id, "dataset_id": dataset_id},
            )
            if result.get("status") != "ok":
                return {"contents": [], "structuredContent": result}
            data = result.get("data")
            tables = data.get("tables") if isinstance(data, Mapping) else None
            table = next(
                (
                    item
                    for item in tables or ()
                    if isinstance(item, Mapping)
                    and str(item.get("table_name") or "").casefold()
                    in {table_name.casefold(), f"public.{table_name}".casefold()}
                ),
                None,
            )
            if table is None:
                missing = _error(correlation, QueryErrorCode.NOT_FOUND, "Database schema resource was not found").to_dict()
                return {"contents": [], "structuredContent": missing}
            public_resource = {
                "space_id": space_id,
                "dataset_id": dataset_id,
                "table_name": str(table.get("table_name") or table_name),
                "columns": list(table.get("columns") or []),
                "schema_revision": table.get("schema_revision"),
                "source_revision": data.get("source_revision") if isinstance(data, Mapping) else None,
            }
            resource_result = QueryResult(
                status="ok",
                trace_id=correlation.trace_id,
                data={"resource": public_resource},
            ).to_dict()
            return {
                "contents": [
                    {
                        "uri": resource_uri,
                        "mimeType": "application/json",
                        "text": json.dumps(public_resource, ensure_ascii=False, sort_keys=True),
                    }
                ],
                "structuredContent": resource_result,
            }
        space_manifest = _SPACE_MANIFEST_URI_RE.fullmatch(resource_uri)
        collection_resource = _COLLECTION_RESOURCE_URI_RE.fullmatch(resource_uri)
        legacy_dataset_resource = _LEGACY_DATASET_RESOURCE_URI_RE.fullmatch(resource_uri)
        dataset_resource = collection_resource or legacy_dataset_resource
        if space_manifest or dataset_resource:
            space_id = (space_manifest or dataset_resource).group(1)
            if not self._space_visible(principal, space_id):
                return {"contents": [], "structuredContent": _error(correlation, QueryErrorCode.PERMISSION_DENIED, "knowledge Space scope is required").to_dict()}
            if space_manifest:
                result = await self._rest.handle(
                    method="GET", path="/v1/spaces", principal=principal, correlation=correlation
                )
                entries = result.get("data", {}).get("spaces", []) if result.get("status") == "ok" else []
                entry = next((item for item in entries if isinstance(item, Mapping) and item.get("id") == space_id), None)
            else:
                dataset_id = dataset_resource.group(2)
                result = await self._rest.handle(
                    method="GET",
                    path="/v1/collections",
                    principal=principal,
                    correlation=correlation,
                    body={"space_id": space_id},
                )
                entries = result.get("data", {}).get("datasets")
                if not isinstance(entries, list):
                    entries = result.get("data", {}).get("collections", [])
                entry = next(
                    (item for item in entries if isinstance(item, Mapping) and item.get("id") == dataset_id),
                    None,
                )
            if result.get("status") != "ok":
                return {"contents": [], "structuredContent": result}
            if entry is None:
                missing = _error(correlation, QueryErrorCode.NOT_FOUND, "Resource was not found").to_dict()
                return {"contents": [], "structuredContent": missing}
            if space_manifest:
                public_resource = {
                    "id": space_id,
                    "name": _mcp_text(entry.get("name"), space_id),
                    "description": _mcp_text(entry.get("description"), "Local Knowledge Space exposed through the Platform."),
                }
            else:
                dataset_id = dataset_resource.group(2)
                public_resource = {
                    "id": dataset_id,
                    "space_id": space_id,
                    "name": _mcp_text(entry.get("name"), dataset_id),
                    "description": "Platform Collection manifest",
                }
            resource_result = QueryResult(status="ok", trace_id=correlation.trace_id, data={"resource": public_resource})
            resource_payload = resource_result.to_dict()
            return {
                "contents": [
                    {
                        "uri": resource_uri,
                        "mimeType": "application/json",
                        "text": json.dumps(resource_payload["data"], ensure_ascii=False, sort_keys=True),
                    }
                ],
                "structuredContent": resource_payload,
            }
        parts = resource_uri.removeprefix("knowledge://").split("/")
        base_asset = (
            len(parts) == 4
            and parts[0] == "spaces"
            and _ID_RE.fullmatch(parts[1])
            and parts[2] == "assets"
            and _ID_RE.fullmatch(parts[3])
        )
        derivative_asset = (
            len(parts) == 6
            and parts[0] == "spaces"
            and _ID_RE.fullmatch(parts[1])
            and parts[2] == "assets"
            and _ID_RE.fullmatch(parts[3])
            and parts[4] == "derivatives"
            and _ID_RE.fullmatch(parts[5])
        )
        if not is_valid_knowledge_uri(resource_uri) or not (base_asset or derivative_asset):
            return {"contents": [], "structuredContent": _error(correlation, QueryErrorCode.INVALID_REQUEST, "Resource URI is invalid").to_dict()}
        if derivative_asset:
            result = await self._rest.handle(
                method="GET",
                path=f"/v1/assets/{parts[3]}/derivatives/{parts[5]}",
                principal=principal,
                correlation=correlation,
                body={"start": start, "end": end},
            )
        else:
            result = await self._rest.handle(
                method="POST",
                path=f"/v1/assets/{parts[3]}:read",
                principal=principal,
                correlation=correlation,
                body={"resource_uri": resource_uri, "start": start, "end": end},
            )
        if result.get("status") != "ok":
            return {"contents": [], "structuredContent": result}
        data = result.get("data", {})
        if data.get("resource_uri") != resource_uri:
            return {"contents": [], "structuredContent": _error(correlation, QueryErrorCode.INVALID_REQUEST, "Resource URI does not match its Asset binding").to_dict()}
        return {
            "contents": [
                {
                    "uri": resource_uri,
                    "mimeType": data.get("mime_type", "application/octet-stream"),
                    "blob": data.get("content_base64", ""),
                }
            ],
            "structuredContent": result,
        }
