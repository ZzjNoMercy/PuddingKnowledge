"""Owned URL capture, durable ingestion and immutable source snapshots."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import uuid

from sqlalchemy import create_engine, select, text, update
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from knowledge_platform.catalog.models import KnowledgeAsset, KnowledgeIngestionJob, KnowledgeSpace, KnowledgeWebCapture, utcnow
from knowledge_platform.local.objects import LocalObjectStore
from knowledge_platform.local.vault import LocalCredentialStore
from knowledge_platform.wiki.ports import RawSnapshot

SPACE = 'space_kb_default'


def digest(data: bytes) -> str:
    return 'sha256:' + hashlib.sha256(data).hexdigest()


class ReadLaterError(ValueError):
    pass


class ReadLaterService:
    def __init__(self, catalog: Path, state_root: Path, *, allowed_origins=()):
        self.engine = create_engine('sqlite:///' + str(catalog), poolclass=NullPool)
        self.objects = LocalObjectStore(state_root / 'objects')
        with self.engine.begin() as connection:
            connection.exec_driver_sql('CREATE TABLE IF NOT EXISTS knowledge_local_object_store (id INTEGER PRIMARY KEY CHECK(id=1), store_id TEXT NOT NULL)')
            connection.execute(text('INSERT OR IGNORE INTO knowledge_local_object_store VALUES (1,:store_id)'), {'store_id':self.objects.identity})
            if connection.execute(text('SELECT store_id FROM knowledge_local_object_store WHERE id=1')).scalar_one()!=self.objects.identity:
                raise ReadLaterError('Catalog belongs to another object store; restore its owned objects')
        self.vault = LocalCredentialStore(state_root, owner_user_id='read-later')
        self.locks = state_root / 'capture-locks'
        if self.locks.is_symlink():
            raise ReadLaterError('Capture lock root is invalid')
        self.locks.mkdir(mode=0o700, exist_ok=True)
        self.allowed_origins = tuple(allowed_origins)

    @contextmanager
    def _lock(self, capture_id):
        path = self.locks / (capture_id + '.lock')
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ReadLaterError('Invalid capture lock')
            try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError: raise ReadLaterError('Capture is already in progress') from None
            yield
        finally:
            os.close(fd)

    @staticmethod
    def _result(job):
        return {'job_id':job.id, 'capture_id':job.capture_id, 'asset_id':job.asset_id,
                'status':job.status, 'source_uri':job.source_uri,
                'content_digest':job.source_digest, 'attempt':job.attempt}

    async def capture(self, *, url: str, idempotency_key: str, space_id: str = SPACE):
        from knowledge_platform.capture.html import canonicalize_url
        from knowledge_platform.local.capture_fetch import fetch
        if space_id != SPACE or not isinstance(idempotency_key, str) or not idempotency_key.strip() or len(idempotency_key) > 512:
            raise ReadLaterError('Invalid capture identity')
        if not isinstance(url, str) or len(url) > 8192:
            raise ReadLaterError('Invalid capture URL')
        canonical = canonicalize_url(url)
        url_digest = digest(canonical.encode())
        capture_id = 'capture_' + hashlib.sha256((SPACE+'\0'+canonical).encode()).hexdigest()[:48]
        job_id = 'capture_job_' + hashlib.sha256((SPACE+'\0'+idempotency_key).encode()).hexdigest()[:48]
        with self._lock(job_id), self._lock(capture_id):
            with Session(self.engine) as session, session.begin():
                if session.get(KnowledgeSpace, SPACE) is None:
                    raise ReadLaterError('Capture Space is unavailable')
                job = session.get(KnowledgeIngestionJob, job_id)
                if job:
                    if job.capture_id != capture_id or job.metadata_json.get('url_digest') != url_digest:
                        raise ReadLaterError('Idempotency key belongs to another capture')
                    if job.status == 'succeeded':
                        return self._result(job)
                    if job.status not in {'running','failed','queued'}:
                        raise ReadLaterError('Capture job cannot be retried')
                else:
                    reference = self.vault.put(job_id + "-" + url_digest[7:39], json.dumps({"url": url, "idempotency_key": idempotency_key}))
                    job = KnowledgeIngestionJob(id=job_id, space_id=SPACE, kind='web_capture',
                        capture_id=capture_id, file_type='html', metadata_json={'url_digest':url_digest,'url_ref':reference})
                    session.add(job)
                capture = session.get(KnowledgeWebCapture, capture_id)
                if capture is None:
                    capture = KnowledgeWebCapture(id=capture_id,space_id=SPACE,canonical_url_digest=url_digest,
                        original_url_digest=digest(url.encode()))
                    session.add(capture)
                elif capture.space_id != SPACE or capture.canonical_url_digest != url_digest:
                    raise ReadLaterError('Capture identity is owned by another source')
                job.status='running'; job.current_step='fetching'; job.attempt=(job.attempt or 0)+1
                job.started_at=utcnow(); job.updated_at=utcnow(); job.error_message=''
                owner=uuid.uuid4().hex; job.lease_owner=owner
                capture.parse_status='running'; capture.ingestion_job_id=job_id; capture.updated_at=utcnow()
                session.flush()
            try:
                response = await fetch(url, allowed_origins=self.allowed_origins)
                from knowledge_platform.capture.html import extract_article
                media = response.content_type.split(';',1)[0].strip().lower()
                if media in {'text/html','application/xhtml+xml'}:
                    # Preserve original bytes independently; parser decoding never changes the Raw Snapshot.
                    from bs4 import UnicodeDammit
                    decoded = UnicodeDammit(response.body, is_html=True).unicode_markup
                    if decoded is None:
                        raise ReadLaterError('Captured page encoding is unavailable')
                    metadata, markdown = extract_article(decoded, response.url)
                elif media in {'text/plain','text/markdown'}:
                    markdown=response.body.decode('utf-8'); metadata={'title':'Captured document'}
                else:
                    raise ReadLaterError('Unsupported capture media type')
                if not markdown.strip():
                    raise ReadLaterError('Captured page contains no article text')
                raw_digest=self.objects.put(response.body)
                content_digest=self.objects.put(markdown.encode())
                raw_id='raw_' + hashlib.sha256((capture_id+raw_digest).encode()).hexdigest()[:48]
                asset_id='capture_content_' + hashlib.sha256((capture_id+content_digest).encode()).hexdigest()[:48]
                title=str(metadata.get('title') or 'Captured article')[:500]
                with Session(self.engine) as session, session.begin():
                    fenced=session.execute(update(KnowledgeIngestionJob).where(KnowledgeIngestionJob.id==job_id,
                        KnowledgeIngestionJob.lease_owner==owner,KnowledgeIngestionJob.status=='running').values(current_step='publishing'))
                    if fenced.rowcount!=1:
                        raise ReadLaterError('Capture claim is no longer owned')
                    job=session.get(KnowledgeIngestionJob,job_id)
                    capture=session.get(KnowledgeWebCapture,capture_id)
                    for identity, kind, mime, body_digest in ((raw_id,'raw_snapshot',media,raw_digest),(asset_id,'document','text/markdown',content_digest)):
                        uri=f'knowledge://spaces/{SPACE}/assets/{identity}'
                        asset=session.get(KnowledgeAsset,identity)
                        if asset is None:
                            session.add(KnowledgeAsset(id=identity,space_id=SPACE,kind=kind,title=title,
                                mime_type=mime,source_type='web_capture',source_uri=uri,
                                revision=body_digest,content_digest=body_digest,metadata_json={'capture_id':capture_id}))
                        elif (asset.space_id,asset.source_type,asset.content_digest,asset.source_uri)!=(SPACE,'web_capture',body_digest,uri):
                            raise ReadLaterError('Capture Asset is owned by another source')
                    capture.title=title; capture.site_name=str(metadata.get('site_name') or '')[:300]
                    capture.author=str(metadata.get('author') or '')[:300]
                    capture.content_uri=f'knowledge://spaces/{SPACE}/assets/{asset_id}'
                    capture.raw_snapshot_uri=f'knowledge://spaces/{SPACE}/assets/{raw_id}'
                    capture.asset_id=asset_id; capture.content_digest=content_digest
                    capture.parse_status='succeeded'; capture.fetched_at=utcnow(); capture.updated_at=utcnow(); capture.error_message=''
                    job.asset_id=asset_id; job.source_uri=capture.content_uri; job.source_digest=content_digest
                    job.file_size=len(markdown.encode()); job.title=title[:300]; job.status='succeeded'
                    job.current_step='published'; job.progress=100; job.finished_at=utcnow(); job.updated_at=utcnow()
                    session.flush()
                    result=self._result(job)
                return result
            except BaseException:
                try:
                    with Session(self.engine) as session, session.begin():
                        failed=session.execute(update(KnowledgeIngestionJob).where(KnowledgeIngestionJob.id==job_id,
                            KnowledgeIngestionJob.lease_owner==owner,KnowledgeIngestionJob.status=='running').values(
                            status='failed',error_message='Capture failed; retry the same request',updated_at=utcnow()))
                        capture=session.get(KnowledgeWebCapture,capture_id)
                        if failed.rowcount==1 and capture is not None and capture.ingestion_job_id==job_id:
                            capture.parse_status='failed'; capture.error_message='Capture failed; retry the same request'; capture.updated_at=utcnow()
                except Exception:
                    # A temporarily unavailable Catalog cannot replace the original
                    # failure; the retained job can be reclaimed on the next retry.
                    pass
                raise

    def list(self):
        with Session(self.engine) as session:
            rows=session.scalars(select(KnowledgeWebCapture).where(KnowledgeWebCapture.space_id==SPACE).order_by(KnowledgeWebCapture.created_at.desc()).limit(200))
            return [{'capture_id':row.id,'title':row.title,'status':row.parse_status,'reading_status':row.reading_status,
                     'asset_id':row.asset_id,'content_uri':row.content_uri,'raw_snapshot_uri':row.raw_snapshot_uri,
                     'content_digest':row.content_digest,'job_id':row.ingestion_job_id} for row in rows]

    async def retry(self, job_id):
        with Session(self.engine) as session:
            job=session.get(KnowledgeIngestionJob,job_id)
            if job is None or job.space_id!=SPACE or job.kind!='web_capture':
                raise ReadLaterError('Capture job is unavailable')
            stored = json.loads(self.vault.get(job.metadata_json['url_ref']))
        return await self.capture(url=stored['url'], idempotency_key=stored['idempotency_key'])

    def read_published(self, resource_uri):
        with Session(self.engine) as session:
            asset=session.get(KnowledgeAsset,resource_uri.rsplit('/',1)[-1])
            if asset is None or asset.space_id!=SPACE or asset.source_type!='web_capture' or asset.source_uri!=resource_uri:
                raise LookupError('Captured Asset is unavailable')
            return self.objects.read(asset.content_digest)

    async def get(self, *, snapshot_id, source_revision):
        with Session(self.engine) as session:
            asset=session.get(KnowledgeAsset,snapshot_id)
            if (asset is None or asset.space_id!=SPACE or asset.source_type!='web_capture'
                    or asset.kind!='document' or asset.revision!=source_revision):
                raise ValueError('Captured source snapshot is unavailable')
            return RawSnapshot(asset.id,asset.revision,asset.source_uri,self.objects.read(asset.content_digest).decode('utf-8'),asset.content_digest)


class CaptureBlobReader:
    def __init__(self, repository, captures, fallback):
        self.repository,self.captures,self.fallback=repository,captures,fallback
    async def read(self, request):
        from knowledge_contracts import BlobReadResult
        from knowledge_contracts.artifacts import MAX_BLOB_READ_BYTES
        asset=self.repository.get_asset(asset_id=request.resource_uri.rsplit('/',1)[-1])
        if not asset or asset.get('source_type')!='web_capture':
            return await self.fallback.read(request)
        data=self.captures.read_published(request.resource_uri)
        end=min(len(data),request.end if request.end is not None else request.start+MAX_BLOB_READ_BYTES)
        selected=data[request.start:end]
        return BlobReadResult(resource_uri=request.resource_uri,content=selected,content_digest=digest(selected),
            start=request.start,end=request.start+len(selected),asset_digest=digest(data))


class CaptureAndConfiguredSnapshots:
    def __init__(self, captures, configured):
        self.captures,self.configured=captures,configured
    async def get(self, *, snapshot_id, source_revision):
        with Session(self.captures.engine) as session:
            asset=session.get(KnowledgeAsset,snapshot_id)
            owned=asset is not None and asset.source_type=='web_capture'
        target=self.captures if owned else self.configured
        return await target.get(snapshot_id=snapshot_id,source_revision=source_revision)
