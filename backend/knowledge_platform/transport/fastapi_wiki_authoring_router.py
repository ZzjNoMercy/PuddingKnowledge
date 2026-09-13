"""Bounded HTTP input for explicit Wiki authoring administration."""
import json
import uuid
from fastapi import APIRouter, Depends, Request
from starlette.concurrency import run_in_threadpool
from knowledge_contracts import QueryError, QueryErrorCode, QueryResult
from .fastapi_router import _resolve

MAX_REQUEST_BYTES=32*1024*1024


def decode_request(data):
    def pairs(items):
        result={}
        for key,value in items:
            if key in result:raise ValueError('Duplicate JSON key')
            result[key]=value
        return result
    value=json.loads(data,object_pairs_hook=pairs,parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Invalid number')))
    stack=[(value,0)];count=0
    while stack:
        item,depth=stack.pop();count+=1
        if depth>32 or count>100000:raise ValueError('JSON structure budget exceeded')
        if isinstance(item,dict):stack.extend((v,depth+1) for v in item.values())
        elif isinstance(item,list):stack.extend((v,depth+1) for v in item)
    return value


def create_wiki_authoring_router(service, *, principal_provider):
    router=APIRouter(prefix='/v1/wiki/authoring',tags=['knowledge-authoring'])
    async def principal():return await _resolve(principal_provider())

    @router.post('/{action}')
    async def authoring(action: str, request: Request, who=Depends(principal)):
        trace=uuid.uuid4().hex
        def error(code,message):return QueryResult(status='error',trace_id=trace,error=QueryError(code=code,message=message)).to_dict()
        if who.tenant_id is not None or 'knowledge.admin' not in who.scopes:
            return error(QueryErrorCode.PERMISSION_DENIED,'Wiki authoring admin and exact Space scope are required')
        if action not in ('context','preview','apply'):return error(QueryErrorCode.INVALID_REQUEST,'Unknown authoring action')
        if request.headers.get("content-type", "").split(";",1)[0].strip().lower() != "application/json":
            return error(QueryErrorCode.INVALID_REQUEST,"Authoring requests require application/json")
        try:
            data=bytearray()
            async for chunk in request.stream():
                if len(data)+len(chunk)>MAX_REQUEST_BYTES:raise ValueError('Request budget exceeded')
                data.extend(chunk)
            body=decode_request(data)
            result=await run_in_threadpool(getattr(service,action),who,body)
        except PermissionError:
            return error(QueryErrorCode.PERMISSION_DENIED,'Wiki authoring admin and exact Space scope are required')
        except (ValueError,TypeError,RecursionError,UnicodeError):
            return error(QueryErrorCode.INVALID_REQUEST,'Wiki authoring request rejected; check selection, revision, schema and patch')
        except Exception:
            return error(QueryErrorCode.BINDING_UNAVAILABLE,'Wiki authoring state is unavailable or inconsistent')
        return QueryResult(status='ok',trace_id=trace,data=result).to_dict()
    return router
