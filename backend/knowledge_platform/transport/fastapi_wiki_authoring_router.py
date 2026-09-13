"""Bounded HTTP input for explicit Wiki authoring administration."""
from knowledge_platform.wiki.json_input import decode_request
import uuid
from fastapi import APIRouter, Depends, Request
from starlette.concurrency import run_in_threadpool
from knowledge_contracts import QueryError, QueryErrorCode, QueryResult
from .fastapi_router import _resolve

MAX_REQUEST_BYTES=32*1024*1024




def create_wiki_authoring_router(service, *, principal_provider):
    router=APIRouter(prefix='/v1/wiki/authoring',tags=['knowledge-authoring'])
    async def principal():return await _resolve(principal_provider())

    @router.post('/{action}')
    async def authoring(action: str, request: Request, who=Depends(principal)):
        trace=uuid.uuid4().hex
        def error(code,message):return QueryResult(status='error',trace_id=trace,error=QueryError(code=code,message=message)).to_dict()
        if who.tenant_id is not None or 'knowledge.admin' not in who.scopes:
            return error(QueryErrorCode.PERMISSION_DENIED,'Wiki authoring admin and exact Space scope are required')
        if action not in ('context','preview','apply','generate','proposal','abandon'):return error(QueryErrorCode.INVALID_REQUEST,'Unknown authoring action')
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
