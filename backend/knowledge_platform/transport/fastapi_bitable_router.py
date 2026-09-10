"""Bitable control-plane policy and read-only, non-persistent live query edges."""
from fastapi import APIRouter, Body, Depends, Response
from knowledge_contracts import Correlation, Principal, QueryError, QueryErrorCode, QueryResult
from .bitable_adapters import BitableReadAdapter, allowed
from .fastapi_router import _resolve

SPACE='space_kb_default'


def create_bitable_router(service, *, principal_provider):
    router=APIRouter(prefix='/v1',tags=['knowledge-bitable'])
    async def principal():return await _resolve(principal_provider())
    read = BitableReadAdapter(service)
    def error(code,message):return QueryResult(status='error',trace_id='local-bitable',error=QueryError(code=code,message=message)).to_dict()
    def ok(data):return QueryResult(status='ok',trace_id='local-bitable',data=data).to_dict()
    @router.get('/bitable/sources')
    async def sources(who:Principal=Depends(principal)):
        return await read.handle('list_sources',{},principal=who,correlation=Correlation('local-bitable'))
    @router.get('/sources/{source_id}/bitable/policy')
    async def policy(source_id:str,who:Principal=Depends(principal)):
        if not allowed(who,'admin'):return error(QueryErrorCode.PERMISSION_DENIED,'Bitable Admin and Space scope are required')
        try:
            service.authorize(source_id,who.subject_id)
            return ok(service.policy(source_id))
        except Exception:return error(QueryErrorCode.CAPABILITY_UNAVAILABLE,'Bitable policy is unavailable')
    @router.put('/sources/{source_id}/bitable/policy')
    async def configure(source_id:str,body:dict=Body(...),who:Principal=Depends(principal)):
        if not allowed(who,'admin'):return error(QueryErrorCode.PERMISSION_DENIED,'Bitable Admin and Space scope are required')
        if set(body)!={'policy','expected_revision'}:return error(QueryErrorCode.INVALID_REQUEST,'Bitable policy fields are invalid')
        try:
            service.authorize(source_id,who.subject_id)
            return ok(await service.configure(source_id,**body))
        except Exception:return error(QueryErrorCode.CAPABILITY_UNAVAILABLE,'Bitable policy changed or could not be validated')
    @router.post('/sources/{source_id}/bitable/resolve')
    async def resolve(source_id:str,body:dict=Body(...),who:Principal=Depends(principal)):
        if not allowed(who,'admin'):return error(QueryErrorCode.PERMISSION_DENIED,'Bitable Admin and Space scope are required')
        if set(body)!={'url'}:return error(QueryErrorCode.INVALID_REQUEST,'Bitable reference fields are invalid')
        try:
            service.authorize(source_id,who.subject_id)
            return ok(await service.resolve(source_id,body['url']))
        except Exception:return error(QueryErrorCode.CAPABILITY_UNAVAILABLE,'Bitable link is outside the registered source or unavailable')
    @router.get('/sources/{source_id}/bitable/tables/{table_id}/schema')
    async def schema(source_id:str,table_id:str,response:Response,who:Principal=Depends(principal)):
        response.headers['Cache-Control']='no-store'
        return await read.handle('describe',{'source_id':source_id,'table_id':table_id},principal=who,correlation=Correlation('local-bitable'))
    @router.get('/sources/{source_id}/bitable/relations')
    async def relations(source_id:str,who:Principal=Depends(principal)):
        return await read.handle('relations',{'source_id':source_id},principal=who,correlation=Correlation('local-bitable'))
    @router.post('/sources/{source_id}/bitable/query')
    async def query(source_id:str,response:Response,body:dict=Body(...),who:Principal=Depends(principal)):
        response.headers['Cache-Control']='no-store'
        if 'source_id' in body:return error(QueryErrorCode.INVALID_REQUEST,'Source identity belongs in the route')
        return await read.handle('query',{'source_id':source_id,**body},principal=who,correlation=Correlation('local-bitable'))
    return router
