"""Durable host-bound file processing and atomic local publication."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import uuid

from sqlalchemy import create_engine, select, text, update, delete
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from knowledge_contracts import Correlation, Principal, QueryErrorCode, QueryResult
from knowledge_platform.catalog.models import KnowledgeAsset, KnowledgeIngestionJob, KnowledgeSpace, KnowledgeDataset, KnowledgeCollectionBinding, utcnow
from knowledge_platform.ingestion.admin import AssetUploadRequest, _authorized, _error, _ID_RE
from knowledge_platform.local.objects import LocalObjectStore, MAX_BYTES
from knowledge_platform.retrieval.local import _open_regular_file


def digest(content): return 'sha256:'+hashlib.sha256(content).hexdigest()
def encoded(value): return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()
def identity(value): return hashlib.sha256(encoded(value)).hexdigest()


def load_file_config(path):
    raw=json.loads(Path(path).read_text())
    if not isinstance(raw,dict) or set(raw)!={'version','bindings','parsers','collection_id'} or type(raw['version']) is not int or raw['version']!=1:
        raise ValueError('Invalid file configuration')
    if not isinstance(raw['collection_id'],str) or not _ID_RE.fullmatch(raw['collection_id']):raise ValueError('Invalid file Collection identity')
    if not isinstance(raw['bindings'],list) or not 1<=len(raw['bindings'])<=1000:raise ValueError('Invalid file bindings')
    seen=set()
    for binding in raw['bindings']:
        if not isinstance(binding,dict) or not {'id','path','space_id'}<=set(binding) or set(binding)-{'id','path','space_id','parser_id'}:raise ValueError('Invalid file binding')
        if any(not isinstance(binding[x],str) or not _ID_RE.fullmatch(binding[x]) for x in ('id','space_id')) or binding['id'] in seen:raise ValueError('Invalid file binding identity')
        seen.add(binding['id'])
        if not isinstance(binding['path'],str) or not Path(binding['path']).is_absolute() or '..' in Path(binding['path']).parts:raise ValueError('File binding must be absolute')
        if 'parser_id' in binding and (not isinstance(binding['parser_id'],str) or not _ID_RE.fullmatch(binding['parser_id'])):raise ValueError('Invalid parser selection')
    from knowledge_platform.parsers.registry import build_registry
    build_registry(raw['parsers'])
    return raw


class LocalFileService:
    def __init__(self,config,catalog,state_root):
        from knowledge_platform.parsers.registry import build_registry
        from knowledge_platform.local.file_index import create_schema
        self.config=json.loads(encoded(config));self.config_digest=digest(encoded(config))
        self.bindings={x['id']:x for x in config['bindings']}
        self.registry=build_registry(config['parsers'])
        self.engine=create_engine('sqlite:///'+str(catalog),poolclass=NullPool)
        self.objects=LocalObjectStore(state_root/'objects')
        with self.engine.begin() as connection:
            connection.exec_driver_sql('CREATE TABLE IF NOT EXISTS knowledge_local_object_store (id INTEGER PRIMARY KEY CHECK(id=1), store_id TEXT NOT NULL)')
            connection.execute(text('INSERT OR IGNORE INTO knowledge_local_object_store VALUES (1,:store)'),{'store':self.objects.identity})
            if connection.execute(text('SELECT store_id FROM knowledge_local_object_store WHERE id=1')).scalar_one()!=self.objects.identity:raise ValueError('File Catalog object store mismatch')
        create_schema(self.engine)
        locks=state_root/'file-locks'
        if locks.is_symlink():raise ValueError('Invalid file lock root')
        locks.mkdir(mode=0o700,exist_ok=True)
        self._locks_fd=os.open(locks,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)

    def close(self):
        os.close(self._locks_fd);self.objects.close();self.engine.dispose()

    @contextmanager
    def _lock(self,key):
        fd=os.open(identity(key)+'.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|os.O_NONBLOCK,0o600,dir_fd=self._locks_fd)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):raise ValueError('Invalid file lock')
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise ValueError('File processing is already running') from None
            yield
        finally:os.close(fd)

    @staticmethod
    def _asset_version(asset):
        return None if asset is None else identity([asset.space_id,asset.source_type,asset.revision,asset.content_digest,asset.metadata_json])

    def _binding(self,request):
        binding=self.bindings.get(request.binding_id)
        if binding is None or binding['space_id']!=request.space_id:raise ValueError('File binding unavailable in this Space')
        return binding

    def _published(self,session,asset_id):
        asset=session.get(KnowledgeAsset,asset_id)
        if asset is None or asset.source_type!='local_file':raise LookupError('File Asset unavailable')
        original=session.get(KnowledgeAsset,asset.metadata_json.get('original_asset_id',asset.id))
        if original is None or original.source_type!='local_file' or original.kind!='original_file' or original.space_id!=asset.space_id:raise LookupError('File original unavailable')
        job=session.get(KnowledgeIngestionJob,original.metadata_json.get('job_id'))
        if job is None or job.kind!='file_import' or job.status!='succeeded' or job.asset_id!=original.id or job.source_digest!=original.content_digest:raise LookupError('File is not published')
        if asset.id!=original.id and (asset.id not in original.metadata_json.get('published_asset_ids',[]) or asset.metadata_json.get('job_id')!=job.id):raise LookupError('File derivative is no longer current')
        return asset

    def is_current(self,asset_id):
        try:
            with Session(self.engine) as session:self._published(session,asset_id)
            return True
        except LookupError:return False

    def normalized_original(self,asset_id):
        try:
            with Session(self.engine) as session:
                asset=self._published(session,asset_id)
                if asset.kind!='document':return None
                return asset.metadata_json.get('original_asset_id')
        except LookupError:return None

    def index_binding(self,space_id):
        with Session(self.engine) as session:
            query=select(KnowledgeDataset).where(KnowledgeDataset.id==self.config['collection_id'])
            if space_id is not None:query=query.where(KnowledgeDataset.space_id==space_id)
            rows=list(session.scalars(query))
            owned = []
            for row in rows:
                binding = session.get(KnowledgeCollectionBinding, (row.space_id, row.id, row.version, 'document_rag_query'))
                if binding is not None and binding.binding_json == {'provider_id': 'knowledge_package'}:
                    continue
                if binding is None or binding.binding_json != {'provider_id': 'knowledge_local_files'}:
                    raise ValueError('File Collection provider binding invalid')
                owned.append(row)
            rows = owned
            if not rows:return None
            if len({x.space_id for x in rows})!=len(rows) or any(x.kind!='local_file_index' for x in rows):raise ValueError('File Collection binding invalid')
            return {'asset_ids':sorted({asset_id for row in rows for asset_id in row.asset_ids}),
                'chunk_count':sum(row.freshness['chunk_count'] for row in rows)}

    def read_published(self,uri):
        with Session(self.engine) as session:
            asset=self._published(session,uri.rsplit('/',1)[-1])
            if asset.source_uri!=uri:raise LookupError('File URI mismatch')
            return self.objects.read(asset.content_digest)

    def derivative_targets(self,asset_id):
        with Session(self.engine) as session:
            asset=session.get(KnowledgeAsset,asset_id)
            if asset is None or asset.source_type!='local_file':return {}
            asset=self._published(session,asset_id)
            targets=asset.metadata_json.get('derivatives',{})
            for target_id in targets.values():self._published(session,target_id)
            return dict(targets)

    def _result(self,job):
        return {'job_id':job.id,'asset_id':job.asset_id,'space_id':job.space_id,'status':job.status,
            'content_digest':job.source_digest,'resource_uri':job.source_uri,'attempt':job.attempt,
            **job.metadata_json.get('result',{})}

    async def stage(self,*,principal,correlation,request):
        if not _authorized(principal,request.space_id):return _error(correlation,QueryErrorCode.PERMISSION_DENIED,'File import requires Admin and Space scope')
        try:
            result=await self.import_file(principal=principal,request=request)
            return QueryResult(status='ok',trace_id=correlation.trace_id,data={'upload':result})
        except ValueError:return _error(correlation,QueryErrorCode.INVALID_REQUEST,'File import identity, binding or parser is invalid')
        except Exception:return _error(correlation,QueryErrorCode.CAPABILITY_UNAVAILABLE,'File processing failed; retry the same request')

    async def import_file(self,*,principal,request):
        if not _authorized(principal,request.space_id):raise PermissionError('Admin and Space scope required')
        request=AssetUploadRequest.from_mapping(asdict(request))
        binding=self._binding(request)
        fingerprint=identity([asdict(request),principal.subject_id,self.config_digest])
        job_id='file_job_'+identity([request.space_id,principal.subject_id,request.idempotency_key])[:48]
        with self._lock(job_id),self._lock(request.asset_id):
            with Session(self.engine) as session,session.begin():
                if session.get(KnowledgeSpace,request.space_id) is None:raise ValueError('Space unavailable')
                original=session.get(KnowledgeAsset,request.asset_id)
                if original and (original.source_type!='local_file' or original.kind!='original_file' or original.space_id!=request.space_id):raise ValueError('Asset identity occupied')
                job=session.get(KnowledgeIngestionJob,job_id)
                if job:
                    if job.metadata_json.get('fingerprint')!=fingerprint:raise ValueError('Idempotency key reused with different input')
                    if job.status=='succeeded':return self._result(job)
                    if job.status not in {'running','failed','queued'}:raise ValueError('Job not retryable')
                    previous_version=job.metadata_json['previous_version']
                    if previous_version!=self._asset_version(original):raise ValueError('Source was replaced since this job started')
                else:
                    previous_version=self._asset_version(original)
                    job=KnowledgeIngestionJob(id=job_id,space_id=request.space_id,kind='file_import',asset_id=request.asset_id,
                        file_name=request.filename,file_type=Path(request.filename).suffix,file_size=0,title=request.title,
                        source_digest=request.content_digest,source_uri=f'knowledge://spaces/{request.space_id}/assets/{request.asset_id}',
                        metadata_json={'fingerprint':fingerprint,'previous_version':previous_version,'config_digest':self.config_digest})
                    session.add(job)
                owner=uuid.uuid4().hex
                job.status='running';job.lease_owner=owner;job.attempt=(job.attempt or 0)+1;job.current_step='reading'
                job.started_at=utcnow();job.finished_at=None;job.error_message='';job.updated_at=utcnow()
            try:
                with _open_regular_file(Path(binding['path'])) as fd:
                    if os.fstat(fd).st_size>MAX_BYTES:raise ValueError('File exceeds object bound')
                    content=bytearray()
                    while len(content)<=MAX_BYTES:
                        chunk=os.read(fd,min(65536,MAX_BYTES+1-len(content)))
                        if not chunk:break
                        content.extend(chunk)
                content=bytes(content)
                if len(content)>MAX_BYTES or digest(content)!=request.content_digest:raise ValueError('File differs from requested digest')
                self.objects.put(content)
                parsed=await self.registry.parse(request.filename,content,parser_id=binding.get('parser_id'))
                from knowledge_platform.parsers.mineru import rewrite_media
                if not parsed.markdown.strip() or len(parsed.assets)>512 or sum(len(x.content) for x in parsed.assets)+len(parsed.markdown)>64*1024*1024:raise ValueError('Parser output exceeds bounds')
                publication=identity([job_id,request.content_digest,parsed.parser_id,parsed.version,digest(parsed.markdown),
                    sorted((x.relative_path,x.mime_type,digest(x.content)) for x in parsed.assets)])
                rows=[];replacements={}
                for media in parsed.assets:
                    if media.relative_path in replacements:raise ValueError('Duplicate parsed image')
                    media_id='file_image_'+identity([publication,media.relative_path])[:48]
                    uri=f'knowledge://spaces/{request.space_id}/assets/{media_id}'
                    replacements[media.relative_path]=uri
                    rows.append((media_id,'derived_media',media.mime_type,self.objects.put(media.content),uri))
                markdown=rewrite_media(parsed.markdown,replacements)
                normalized_id='file_document_'+publication[:48]
                normalized_digest=self.objects.put(markdown)
                rows.append((normalized_id,'document','text/markdown',normalized_digest,f'knowledge://spaces/{request.space_id}/assets/{normalized_id}'))
                from knowledge_platform.local.file_index import replace_chunks
                with Session(self.engine) as session,session.begin():
                    fence=session.execute(update(KnowledgeIngestionJob).where(KnowledgeIngestionJob.id==job_id,
                        KnowledgeIngestionJob.lease_owner==owner,KnowledgeIngestionJob.status=='running').values(heartbeat_at=utcnow()))
                    if fence.rowcount!=1:raise ValueError('File job ownership lost')
                    original=session.get(KnowledgeAsset,request.asset_id)
                    if self._asset_version(original)!=previous_version:raise ValueError('File source changed during parsing')
                    if original is None:
                        original=KnowledgeAsset(id=request.asset_id,space_id=request.space_id,kind='original_file',title=request.title,
                            source_type='local_file',source_uri=f'knowledge://spaces/{request.space_id}/assets/{request.asset_id}')
                        session.add(original)
                    original.title=request.title;original.mime_type=request.mime_type;original.revision=request.content_digest;original.content_digest=request.content_digest
                    original.metadata_json={'job_id':job_id,'derivatives':{'normalized_markdown':normalized_id},'published_asset_ids':[r[0] for r in rows]}
                    original.updated_at=utcnow()
                    for asset_id,kind,mime,body_digest,uri in rows:
                        metadata={'original_asset_id':original.id,'job_id':job_id,'source_digest':request.content_digest,'parser_id':parsed.parser_id,'parser_version':parsed.version,'config_digest':self.config_digest}
                        old=session.get(KnowledgeAsset,asset_id)
                        if old is None:session.add(KnowledgeAsset(id=asset_id,space_id=request.space_id,kind=kind,title=request.title,
                            source_type='local_file',source_uri=uri,mime_type=mime,revision=body_digest,content_digest=body_digest,metadata_json=metadata))
                        elif (old.content_digest,old.metadata_json,old.space_id)!=(body_digest,metadata,request.space_id):raise ValueError('File output collision')
                    session.flush()
                    replace_chunks(session,space_id=request.space_id,original_id=original.id,asset_id=normalized_id,content=markdown.decode('utf-8'),digest=normalized_digest)
                    collection=self._collection(session,request.space_id)
                    job=session.get(KnowledgeIngestionJob,job_id);job.status='succeeded';job.progress=100;job.current_step='published'
                    job.file_size=len(content);job.finished_at=utcnow();job.updated_at=utcnow();job.lease_owner=None
                    job.metadata_json={**job.metadata_json,'result':{'normalized_asset_id':normalized_id,'parser_id':parsed.parser_id,
                        'parser_version':parsed.version,'indexed':True,**collection}}
                    result=self._result(job)
                return result
            except BaseException:
                with Session(self.engine) as session,session.begin():
                    session.execute(update(KnowledgeIngestionJob).where(KnowledgeIngestionJob.id==job_id,KnowledgeIngestionJob.lease_owner==owner,
                        KnowledgeIngestionJob.status=='running').values(status='failed',current_step='failed',error_message='file_processing_failed',finished_at=utcnow(),lease_owner=None))
                raise

    def _collection(self,session,space_id):
        collection_id=self.config['collection_id']
        prior=list(session.scalars(select(KnowledgeDataset).where(KnowledgeDataset.space_id==space_id,KnowledgeDataset.id==collection_id)))
        if any(x.kind!='local_file_index' for x in prior):raise ValueError('Collection identity occupied')
        for row in prior:
            binding = session.get(KnowledgeCollectionBinding, (row.space_id, row.id, row.version, 'document_rag_query'))
            if binding is None or binding.binding_json != {'provider_id': 'knowledge_local_files'}:
                raise ValueError('Collection identity belongs to another provider')
        ids=[]
        for asset in session.scalars(select(KnowledgeAsset).where(KnowledgeAsset.space_id==space_id,KnowledgeAsset.source_type=='local_file',KnowledgeAsset.kind=='original_file')):
            ids.extend(asset.metadata_json.get('derivatives',{}).values())
        ids=sorted(set(ids));manifest=digest(encoded([(x,session.get(KnowledgeAsset,x).content_digest) for x in ids]));version=manifest[7:39]
        session.execute(delete(KnowledgeCollectionBinding).where(KnowledgeCollectionBinding.space_id==space_id,KnowledgeCollectionBinding.collection_id==collection_id))
        for old in prior:session.delete(old)
        session.flush()
        session.add(KnowledgeDataset(id=collection_id,space_id=space_id,name='Imported files',version=version,kind='local_file_index',
            asset_ids=ids,capabilities=['document_rag_query'],manifest_digest=manifest,freshness={'status':'fresh','indexed_at':utcnow().isoformat(),'chunk_count':session.execute(text('SELECT count(*) FROM file_chunks WHERE space_id=:space'),{'space':space_id}).scalar_one()}))
        session.add(KnowledgeCollectionBinding(space_id=space_id,collection_id=collection_id,collection_version=version,
            capability='document_rag_query',binding_json={'provider_id':'knowledge_local_files'}))
        return {'collection_id':collection_id,'collection_version':version,'index_manifest_digest':manifest}


class FileBlobReader:
    def __init__(self,repository,service,fallback):self.repository=repository;self.service=service;self.fallback=fallback
    async def read(self,request):
        from knowledge_contracts import BlobReadResult
        from knowledge_contracts.artifacts import MAX_BLOB_READ_BYTES
        asset=self.repository.get_asset(asset_id=request.resource_uri.rsplit('/',1)[-1])
        if asset is None or asset.get('source_type')!='local_file':return await self.fallback.read(request)
        content=self.service.read_published(request.resource_uri)
        end=min(len(content),request.end if request.end is not None else request.start+MAX_BLOB_READ_BYTES)
        selected=content[request.start:end]
        return BlobReadResult(resource_uri=request.resource_uri,content=selected,content_digest=digest(selected),
            asset_digest=digest(content),start=request.start,end=request.start+len(selected))
