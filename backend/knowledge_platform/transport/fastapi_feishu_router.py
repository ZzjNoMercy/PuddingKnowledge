"""Admin-only discovery and sync commands for configured Feishu sources."""
from fastapi import APIRouter, Body, Depends, Query
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
    @router.post('/{source_id}:authorize')
    async def authorize(source_id:str,body:dict=Body(...),who:Principal=Depends(principal)):
        if not allowed(who):return error(QueryErrorCode.PERMISSION_DENIED,'OAuth requires Admin and Space scope')
        if set(body)!={'redirect_uri'} or source_id not in service.config:return error(QueryErrorCode.INVALID_REQUEST,'OAuth source or fields are invalid')
        try:result=await service.oauth.start(source_id,principal_id=who.subject_id,redirect_uri=body['redirect_uri'])
        except Exception:return error(QueryErrorCode.CAPABILITY_UNAVAILABLE,'OAuth authorization could not start')
        return QueryResult(status='ok',trace_id='local-feishu',data={'authorization':result}).to_dict()
    async def complete(state,code,who):
        if not allowed(who):return error(QueryErrorCode.PERMISSION_DENIED,'OAuth callback requires Admin and Space scope')
        try:result=await service.oauth.complete(state=state,code=code,principal_id=who.subject_id)
        except Exception:return error(QueryErrorCode.CAPABILITY_UNAVAILABLE,'OAuth callback failed; start a new authorization')
        return QueryResult(status='ok',trace_id='local-feishu',data={'authorization':result}).to_dict()
    @router.post('/oauth/callback')
    async def oauth_callback(body:dict=Body(...),who:Principal=Depends(principal)):
        if set(body)!={'state','code'}:return error(QueryErrorCode.INVALID_REQUEST,'OAuth callback fields are invalid')
        return await complete(body['state'],body['code'],who)
    @router.get('/oauth/callback')
    async def oauth_browser_callback(state:str=Query(...),code:str=Query(...),who:Principal=Depends(principal)):
        return await complete(state,code,who)
    @router.post('/{source_id}:revoke-authorization')
    async def revoke(source_id:str,who:Principal=Depends(principal)):
        if not allowed(who):return error(QueryErrorCode.PERMISSION_DENIED,'OAuth revocation requires Admin and Space scope')
        if source_id not in service.config:return error(QueryErrorCode.INVALID_REQUEST,'OAuth source is not configured')
        try:result=service.oauth.revoke(source_id,principal_id=who.subject_id)
        except Exception:return error(QueryErrorCode.CAPABILITY_UNAVAILABLE,'Local authorization revocation failed')
        return QueryResult(status='ok',trace_id='local-feishu',data={'authorization':result}).to_dict()
    return router
