"""Opaque document resource identifiers; arbitrary filesystem paths are forbidden."""
import re
from fastapi import APIRouter
from fastapi.responses import JSONResponse,Response
from ..local.document_resources import DocumentResourceError


def create_document_resources_router(service,*,principal_provider):
    router=APIRouter(prefix='/v1/assets',tags=['knowledge-resources'])
    headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Content-Security-Policy':"sandbox; default-src 'none'",'Referrer-Policy':'no-referrer'}
    def error(status):return JSONResponse({'status':'error','error':{'code':'resource_unavailable','message':'Document resource unavailable'}},status_code=status,headers=headers)
    @router.get('/{asset_id}/resources')
    def list_resources(asset_id:str):
        if not re.fullmatch(r'[A-Za-z0-9._:-]{1,160}',asset_id):return error(404)
        try:return JSONResponse({'status':'ok','data':{'resources':service.list(principal_provider(),asset_id)}},headers=headers)
        except DocumentResourceError as exc:return error(exc.status)
    @router.get('/{asset_id}/resources/{resource_id}')
    def read_resource(asset_id:str,resource_id:str):
        if not re.fullmatch(r'[A-Za-z0-9._:-]{1,160}',asset_id) or not re.fullmatch(r'[a-f0-9]{64}',resource_id):return error(404)
        try:
            data,mime=service.read(principal_provider(),asset_id,resource_id)
            disposition='inline' if mime.startswith('image/') else 'attachment'
            return Response(data,media_type=mime,headers={**headers,'Content-Disposition':f'{disposition}; filename="resource-{resource_id}"'})
        except DocumentResourceError as exc:return error(exc.status)
    return router
