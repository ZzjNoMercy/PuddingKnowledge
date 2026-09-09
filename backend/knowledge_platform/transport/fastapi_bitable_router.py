"""Bitable control-plane policy and read-only, non-persistent live query edges."""
from fastapi import APIRouter, Body, Depends, Response
from knowledge_contracts import Evidence, Principal, QueryError, QueryErrorCode, QueryResult
from .fastapi_router import _resolve

SPACE='space_kb_default'


def create_bitable_router(service, *, principal_provider):
    router=APIRouter(prefix='/v1',tags=['knowledge-bitable'])
    async def principal():return await _resolve(principal_provider())
    def allowed(who,operation):
        scopes=set(who.scopes)
        return who.tenant_id is None and bool({f'knowledge.space:{SPACE}',f'knowledge:space:{SPACE}'}&scopes) and bool({'knowledge.admin','knowledge:admin',f'knowledge.{operation}',f'knowledge:{operation}'}&scopes)
    def error(code,message):return QueryResult(status='error',trace_id='local-bitable',error=QueryError(code=code,message=message)).to_dict()
    def ok(data):return QueryResult(status='ok',trace_id='local-bitable',data=data).to_dict()
    @router.get('/bitable/sources')
    async def sources(who:Principal=Depends(principal)):
        if not allowed(who,'query'):return error(QueryErrorCode.PERMISSION_DENIED,'Bitable Query and Space scope are required')
        items=[]
        for source_id in service.owner.config:
            try:
                service.authorize(source_id,who.subject_id)
                policy=service.policy(source_id)
            except Exception:continue
            items.append({'source_id':source_id,**policy})
        return ok({'sources':items,'row_storage':False})
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
        if not allowed(who,'query'):return error(QueryErrorCode.PERMISSION_DENIED,'Bitable Query and Space scope are required')
        try:
            service.authorize(source_id,who.subject_id)
            return ok(await service.describe(source_id,table_id))
        except Exception:return error(QueryErrorCode.CAPABILITY_UNAVAILABLE,'Bitable schema is unavailable')
    @router.get('/sources/{source_id}/bitable/relations')
    async def relations(source_id:str,who:Principal=Depends(principal)):
        if not allowed(who,'query'):return error(QueryErrorCode.PERMISSION_DENIED,'Bitable Query and Space scope are required')
        try:
            service.authorize(source_id,who.subject_id)
            return ok(service.relations(source_id))
        except Exception:return error(QueryErrorCode.CAPABILITY_UNAVAILABLE,'Bitable relation schema is unavailable')
    @router.post('/sources/{source_id}/bitable/query')
    async def query(source_id:str,response:Response,body:dict=Body(...),who:Principal=Depends(principal)):
        response.headers['Cache-Control']='no-store'
        if not allowed(who,'query'):return error(QueryErrorCode.PERMISSION_DENIED,'Bitable Query and Space scope are required')
        if set(body)!={'table_id','schema_revision','field_names','page_size','cursor'}:
            return error(QueryErrorCode.INVALID_REQUEST,'Bitable query fields are invalid')
        try:result=await service.query(source_id,**body,principal_id=who.subject_id)
        except Exception:return error(QueryErrorCode.CAPABILITY_UNAVAILABLE,'Bitable query failed or schema/scope changed')
        return QueryResult(status='ok',trace_id='local-bitable',data=result,evidence=(Evidence(
            asset_id=result['schema_asset_id'],resource_uri=result['schema_resource_uri'],
            locator={'section':'bitable_schema'},revision='sha256:'+result['schema_revision'],matched_by=('live_query',)),)).to_dict()
    return router
