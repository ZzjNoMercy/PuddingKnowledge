"""Admin-only discovery and sync commands for configured Feishu sources."""
from fastapi import APIRouter, Body, Depends
from knowledge_contracts import Principal, QueryError, QueryErrorCode, QueryResult
from .fastapi_router import _resolve

SPACE='space_kb_default'


def create_feishu_router(service, *, principal_provider):
    router=APIRouter(prefix='/v1/sources',tags=['knowledge-admin'])
    async def principal(): return await _resolve(principal_provider())
    def allowed(who):
        return who.tenant_id is None and bool({'knowledge.admin','knowledge:admin'}&set(who.scopes)) and bool({f'knowledge.space:{SPACE}',f'knowledge:space:{SPACE}'}&set(who.scopes))
    def error(code,message):
        return QueryResult(status='error',trace_id='local-feishu',error=QueryError(code=code,message=message)).to_dict()
    @router.post('/{source_id}:discover')
    async def discover(source_id:str,who:Principal=Depends(principal)):
        if not allowed(who): return error(QueryErrorCode.PERMISSION_DENIED,'Source discovery requires Admin and Space scope')
        try: entries=await service.discover(source_id)
        except Exception: return error(QueryErrorCode.CAPABILITY_UNAVAILABLE,'Feishu source discovery failed')
        return QueryResult(status='ok',trace_id='local-feishu',data={'entries':entries}).to_dict()
    @router.post('/{source_id}:sync')
    async def sync(source_id:str,body:dict=Body(...),who:Principal=Depends(principal)):
        if not allowed(who): return error(QueryErrorCode.PERMISSION_DENIED,'Source sync requires Admin and Space scope')
        if set(body)!={'idempotency_key','mode'}: return error(QueryErrorCode.INVALID_REQUEST,'Sync fields are invalid')
        try: result=await service.sync(source_id,**body)
        except Exception: return error(QueryErrorCode.CAPABILITY_UNAVAILABLE,'Feishu sync failed; retry the original request')
        return QueryResult(status='ok',trace_id='local-feishu',data={'sync':result}).to_dict()
    return router
