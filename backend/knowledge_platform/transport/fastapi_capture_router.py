"""Explicit Admin edges for independently owned Read Later ingestion."""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends
from knowledge_contracts import Principal, QueryError, QueryErrorCode, QueryResult
SPACE = "space_kb_default"
from knowledge_platform.wiki.compiler import WikiCompilationRequest
from .fastapi_router import _resolve


def create_capture_router(service, *, principal_provider, wiki_compilation=None):
    router=APIRouter(prefix='/v1/captures',tags=['knowledge-admin'])
    async def principal():
        return await _resolve(principal_provider())
    def error(code, message):
        return QueryResult(status='error',trace_id='local-capture',error=QueryError(code=code,message=message)).to_dict()
    def allowed(who, write=True):
        operations={'knowledge.admin','knowledge:admin','knowledge.processing','knowledge:processing'} if write else {'knowledge.read','knowledge:read','knowledge.admin','knowledge:admin'}
        return who.tenant_id is None and bool(operations & set(who.scopes)) and bool({f'knowledge.space:{SPACE}',f'knowledge:space:{SPACE}'} & set(who.scopes))
    def success(data):
        return QueryResult(status='ok',trace_id='local-capture',data=data).to_dict()
    @router.get('')
    async def list_captures(who: Principal=Depends(principal)):
        if not allowed(who,False): return error(QueryErrorCode.PERMISSION_DENIED,'Capture read scope is required')
        return success({'captures':service.list()})
    @router.post('')
    async def capture(body: dict=Body(...),who: Principal=Depends(principal)):
        if not allowed(who): return error(QueryErrorCode.PERMISSION_DENIED,'Capture processing and Space scope are required')
        if set(body)!={'url','idempotency_key','space_id'}: return error(QueryErrorCode.INVALID_REQUEST,'Capture fields are invalid')
        try: result=await service.capture(**body)
        except Exception: return error(QueryErrorCode.CAPABILITY_UNAVAILABLE,'Capture failed; retry the same request')
        return success({'capture':result})
    @router.post('/jobs/{job_id}:retry')
    async def retry(job_id:str,who: Principal=Depends(principal)):
        if not allowed(who): return error(QueryErrorCode.PERMISSION_DENIED,'Capture processing and Space scope are required')
        try: result=await service.retry(job_id)
        except Exception: return error(QueryErrorCode.CAPABILITY_UNAVAILABLE,'Capture retry failed')
        return success({'capture':result})
    @router.post('/assets/{asset_id}:promote')
    async def promote(asset_id:str,body:dict=Body(...),who:Principal=Depends(principal)):
        if not allowed(who): return error(QueryErrorCode.PERMISSION_DENIED,'Capture processing and Space scope are required')
        if wiki_compilation is None: return error(QueryErrorCode.CAPABILITY_UNAVAILABLE,'Wiki compilation is unavailable')
        if set(body)!={'source_revision','idempotency_key'}: return error(QueryErrorCode.INVALID_REQUEST,'Promotion fields are invalid')
        try:
            snapshot=await service.get(snapshot_id=asset_id,source_revision=body['source_revision'])
            result=await wiki_compilation.compile(WikiCompilationRequest(snapshot.snapshot_id,snapshot.source_revision,snapshot.source_uri,snapshot.content_digest,body['idempotency_key']))
        except Exception: return error(QueryErrorCode.CAPABILITY_UNAVAILABLE,'Capture promotion failed')
        return success({'compilation':{'snapshot_id':result.snapshot_id,'source_revision':result.source_revision,'resource_uri':result.resource_uri}})
    return router
