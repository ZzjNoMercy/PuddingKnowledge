"""Read-only job and parser status for the host-bound file import edge."""
from fastapi import APIRouter,Depends
from sqlalchemy.orm import Session
from knowledge_contracts import Principal,QueryError,QueryErrorCode,QueryResult
from knowledge_platform.catalog.models import KnowledgeIngestionJob
from knowledge_platform.ingestion.admin import _authorized
from .fastapi_router import _resolve


def create_files_router(service,*,principal_provider):
    router=APIRouter(prefix='/v1/files',tags=['knowledge-admin'])
    async def principal():return await _resolve(principal_provider())
    def error(code,message):return QueryResult(status='error',trace_id='file-job',error=QueryError(code=code,message=message)).to_dict()
    @router.get('/jobs/{job_id}')
    async def job(job_id:str,who:Principal=Depends(principal)):
        with Session(service.engine) as session:
            row=session.get(KnowledgeIngestionJob,job_id)
            if row is None or row.kind!='file_import':return error(QueryErrorCode.NOT_FOUND,'File job not found')
            if not _authorized(who,row.space_id):return error(QueryErrorCode.PERMISSION_DENIED,'Admin and Space scope required')
            return QueryResult(status='ok',trace_id='file-job',data={'job':service._result(row)}).to_dict()
    return router
