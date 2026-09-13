"""Read-only reconciliation of historical and current Wiki processing evidence."""
import re
import uuid
from fastapi import APIRouter, Depends
from knowledge_contracts import Principal, QueryError, QueryErrorCode, QueryResult
from .fastapi_router import _resolve


def create_wiki_state_router(service, *, principal_provider):
    router = APIRouter(prefix='/v1/wiki/assets', tags=['knowledge-processing'])
    async def principal():
        return await _resolve(principal_provider())

    @router.get('/{asset_id}/processing')
    async def processing_state(asset_id: str, who: Principal = Depends(principal)):
        trace_id = uuid.uuid4().hex
        def error(code, message):
            return QueryResult(status='error', trace_id=trace_id, error=QueryError(code=code, message=message)).to_dict()
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,159}', asset_id):
            return error(QueryErrorCode.INVALID_REQUEST, 'Invalid Asset identifier')
        try:
            result = service.read(asset_id, who)
        except PermissionError:
            return error(QueryErrorCode.PERMISSION_DENIED, 'Wiki state read and target Space scope are required')
        except Exception:
            return error(QueryErrorCode.BINDING_UNAVAILABLE, 'Wiki processing evidence is unavailable or inconsistent')
        return QueryResult(status='ok', trace_id=trace_id, data={'processing': result}).to_dict()
    return router
