"""Shared read boundary for Bitable REST and optional external MCP tools."""
from collections.abc import Mapping

from knowledge_contracts import Evidence, QueryError, QueryErrorCode, QueryResult
from .query_adapters import McpQueryAdapter

SPACE = 'space_kb_default'
PREFIX = 'feishu_bitable_'


def allowed(principal, operation='query'):
    scopes = set(principal.scopes)
    return principal.tenant_id is None and bool({f'knowledge.space:{SPACE}', f'knowledge:space:{SPACE}'} & scopes) and bool({'knowledge.admin', 'knowledge:admin', f'knowledge.{operation}', f'knowledge:{operation}'} & scopes)


class BitableReadAdapter:
    def __init__(self, service):
        self.service = service

    async def handle(self, operation, arguments, *, principal, correlation):
        def error(code, message):
            return QueryResult(status='error', trace_id=correlation.trace_id, error=QueryError(code=code, message=message)).to_dict()
        if not allowed(principal):
            return error(QueryErrorCode.PERMISSION_DENIED, 'Bitable Query and Space scope are required')
        fields = {
            'list_sources': set(), 'describe': {'source_id', 'table_id'},
            'relations': {'source_id'},
            'query': {'source_id', 'table_id', 'schema_revision', 'field_names', 'page_size', 'cursor'},
        }
        if operation not in fields:
            return error(QueryErrorCode.NOT_FOUND, 'Bitable read operation is unavailable')
        if not isinstance(arguments, Mapping) or set(arguments) != fields[operation]:
            return error(QueryErrorCode.INVALID_REQUEST, 'Bitable read arguments are invalid')
        evidence = ()
        try:
            if operation == 'list_sources':
                sources = []
                for source_id in self.service.owner.config:
                    try:
                        self.service.authorize(source_id, principal.subject_id)
                        policy = self.service.policy(source_id)
                    except Exception:
                        continue
                    sources.append({'source_id': source_id, **policy})
                result = {'sources': sources, 'row_storage': False}
            else:
                source_id = arguments['source_id']
                if not isinstance(source_id, str):
                    return error(QueryErrorCode.INVALID_REQUEST, 'Bitable source identity is invalid')
                self.service.authorize(source_id, principal.subject_id)
                if operation == 'describe':
                    result = await self.service.describe(source_id, arguments['table_id'])
                elif operation == 'relations':
                    result = self.service.relations(source_id)
                else:
                    body = {key: value for key, value in arguments.items() if key != 'source_id'}
                    result = await self.service.query(source_id, **body, principal_id=principal.subject_id)
                    evidence = (Evidence(asset_id=result['schema_asset_id'], resource_uri=result['schema_resource_uri'],
                        locator={'section': 'bitable_schema'}, revision='sha256:' + result['schema_revision'], matched_by=('live_query',)),)
            return QueryResult(status='ok', trace_id=correlation.trace_id, data=result, evidence=evidence).to_dict()
        except Exception:
            return error(QueryErrorCode.CAPABILITY_UNAVAILABLE, 'Bitable source is unavailable or schema/authorization changed')

    @staticmethod
    def tool_descriptors():
        source = {'type': 'string', 'minLength': 1, 'maxLength': 120}
        table = {'type': 'string', 'pattern': '^[A-Za-z0-9_-]{1,220}$'}
        descriptions = {
            'list_sources': 'List registered readable Bitable sources and approved tables. Empty scope means no access.',
            'describe': 'Read the current table schema and schema_revision. Synchronization is required when sync_required is true.',
            'relations': 'Read explicitly declared relationships and schema-only validation. This does not prove row uniqueness or execute joins.',
            'query': 'Read one live record page from an approved table and configured view. Use the synchronized schema_revision and exact field names. Rows are not stored by Platform; evidence references schema only. Continue only with the returned cursor and unchanged arguments.',
        }
        properties = {
            'list_sources': {}, 'describe': {'source_id': source, 'table_id': table},
            'relations': {'source_id': source},
            'query': {'source_id': source, 'table_id': table,
                'schema_revision': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'},
                'field_names': {'type': 'array', 'maxItems': 100, 'uniqueItems': True, 'items': {'type': 'string', 'minLength': 1, 'maxLength': 500}},
                'page_size': {'type': 'integer', 'minimum': 1, 'maximum': 100},
                'cursor': {'type': 'string', 'maxLength': 8192}},
        }
        return tuple({'name': PREFIX + operation, 'description': descriptions[operation],
            'inputSchema': {'type': 'object', 'properties': props, 'required': list(props), 'additionalProperties': False},
            'annotations': {'readOnlyHint': True, 'destructiveHint': False, 'openWorldHint': True}}
            for operation, props in properties.items())


class BitableMcpQueryAdapter(McpQueryAdapter):
    """Optional Bitable capabilities without changing generic MCP consumers."""
    def __init__(self, rest, *, bitable, **kwargs):
        super().__init__(rest, **kwargs)
        self._bitable = BitableReadAdapter(bitable)

    def tool_descriptors(self):
        return super().tool_descriptors() + self._bitable.tool_descriptors()

    async def call_tool(self, *, name, arguments, principal, correlation):
        if name.startswith(PREFIX):
            result = await self._bitable.handle(name[len(PREFIX):], arguments, principal=principal, correlation=correlation)
            return {'structuredContent': result}
        return await super().call_tool(name=name, arguments=arguments, principal=principal, correlation=correlation)
