"""Persistent local vector indexes with Catalog-bound atomic publication."""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import math
import re
import sqlite3
from urllib.parse import quote

from knowledge_contracts import BlobReadRequest, CitationCandidate, Principal, Correlation, QueryError, QueryErrorCode, QueryResult, validate_blob_read_result
from knowledge_contracts.artifacts import MAX_BLOB_READ_BYTES
from knowledge_platform.retrieval.ports import RetrievalIndexNotReady, RetrievalProviderError

_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$')
_DIGEST = re.compile(r'^sha256:[0-9a-f]{64}$')
_MAX_TOTAL_BYTES = 32 * 1024 * 1024
_MAX_CHUNKS = 10000
_MAX_COMPONENTS = 2_000_000
_CAPS = {'document_rag_query', 'wiki_query'}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _digest(value):
    return 'sha256:' + hashlib.sha256(value).hexdigest()


class LocalVectorIndex:
    def __init__(self, catalog_path, repository, reader, embedder, config):
        self.catalog_path, self.repository, self.reader, self.embedder = catalog_path, repository, reader, embedder
        self.config = json.loads(_json(config))
        self.dimension = self.config['embedding']['dimension']
        self.provider_id = self.config['provider_id']
        self.batch_size, self.max_chars = self.config['batch_size'], self.config['max_chars']
        if self.provider_id != 'knowledge_local_vector' or type(self.dimension) is not int or not 1 <= self.dimension <= 16384:
            raise ValueError('Invalid vector provider configuration')
        if type(self.batch_size) is not int or not 1 <= self.batch_size <= 256 or type(self.max_chars) is not int or not 100 <= self.max_chars <= 12000:
            raise ValueError('Invalid vector chunk configuration')
        embedding = self.config['embedding']
        self.model_identity = _digest(_json({k: embedding[k] for k in ('endpoint','model','dimension')}).encode())
        self._lock = asyncio.Lock()
        with self._db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS knowledge_local_vector_requests (
                    idempotency_key TEXT, subject_id TEXT, scope_json TEXT, fingerprint TEXT, generation INTEGER,
                    PRIMARY KEY(idempotency_key,subject_id));
                CREATE TABLE IF NOT EXISTS knowledge_local_vector_indexes (
                    space_id TEXT, collection_id TEXT, collection_version TEXT, capability TEXT, generation INTEGER,
                    fingerprint TEXT, model_identity TEXT, dimension INTEGER, chunk_count INTEGER, chunks_digest TEXT,
                    status TEXT, PRIMARY KEY(space_id,collection_id,collection_version,capability,generation));
                CREATE TABLE IF NOT EXISTS knowledge_local_vector_chunks (
                    space_id TEXT, collection_id TEXT, collection_version TEXT, capability TEXT, generation INTEGER,
                    ordinal INTEGER, asset_id TEXT, chunk_id TEXT, text TEXT, content_digest TEXT, resource_uri TEXT,
                    embedding_json TEXT, PRIMARY KEY(space_id,collection_id,collection_version,capability,generation,ordinal));
            ''')

    def _db(self, readonly=False):
        db = (sqlite3.connect(f"file:{quote(str(self.catalog_path), safe='/')}?mode=ro",uri=True,timeout=10)
              if readonly else sqlite3.connect(self.catalog_path,timeout=10))
        db.row_factory = sqlite3.Row
        if readonly: db.execute('PRAGMA query_only=ON')
        return db

    def _snapshot(self, key):
        space, cid, version, capability = key
        collection = next((c for c in self.repository.list_collections(space_id=space)
            if c.get('id') == cid and c.get('version') == version), None)
        if collection is None or capability not in collection.get('capabilities', []):
            raise ValueError('Collection is unavailable')
        all_assets = {a['id']:a for a in self.repository.list_assets(space_id=space)}
        assets = []
        for aid in sorted(collection.get('asset_ids', [])):
            asset = all_assets.get(aid)
            if asset is None or asset.get('space_id') != space or not _DIGEST.fullmatch(str(asset.get('content_digest',''))):
                raise ValueError('Collection Asset is unavailable')
            assets.append({k:asset.get(k) for k in ('id','space_id','kind','source_type','source_uri','revision','content_digest','mime_type')})
        facts = {k:collection.get(k) for k in ('id','space_id','version','name','kind','asset_ids','semantic_asset_ids','capabilities')}
        signature = _digest(_json({'collection':facts,'assets':assets,'capability':capability,
            'model_identity':self.model_identity,'max_chars':self.max_chars}).encode())
        return signature, assets, collection

    def _vectors(self, raw, count):
        if not isinstance(raw,(list,tuple)) or len(raw) != count:
            raise ValueError('Embedding count mismatch')
        values=[]
        for vector in raw:
            if not isinstance(vector,(list,tuple)) or len(vector)!=self.dimension:
                raise ValueError('Embedding dimension mismatch')
            if any(type(x) not in (int,float) or not math.isfinite(float(x)) for x in vector):
                raise ValueError('Embedding is not finite')
            norm=math.hypot(*vector)
            if not math.isfinite(norm) or norm<=0:raise ValueError('Embedding is zero or unbounded')
            values.append(tuple(float(x)/norm for x in vector))
        return values

    def _check_index(self, db, row, signature):
        if row is None or row['status']!='active' or row['fingerprint']!=signature or row['model_identity']!=self.model_identity or row['dimension']!=self.dimension:
            raise RetrievalIndexNotReady('Local index identity changed')
        key=tuple(row[k] for k in ('space_id','collection_id','collection_version','capability','generation'))
        size=db.execute('SELECT COUNT(*),COALESCE(SUM(LENGTH(text)+LENGTH(embedding_json)),0) FROM knowledge_local_vector_chunks WHERE space_id=? AND collection_id=? AND collection_version=? AND capability=? AND generation=?',key).fetchone()
        if size[0] > _MAX_CHUNKS or size[0] * self.dimension > _MAX_COMPONENTS or size[1] > _MAX_TOTAL_BYTES:
            raise RetrievalIndexNotReady('Local index exceeds verification budget')
        rows=db.execute('SELECT * FROM knowledge_local_vector_chunks WHERE space_id=? AND collection_id=? AND collection_version=? AND capability=? AND generation=? ORDER BY ordinal',key).fetchall()
        if not rows or len(rows)!=row['chunk_count'] or _digest(_json([tuple(r) for r in rows]).encode())!=row['chunks_digest']:
            raise RetrievalIndexNotReady('Local index chunks are incomplete')
        for ordinal,r in enumerate(rows):
            if r['ordinal']!=ordinal:raise RetrievalIndexNotReady('Local index ordinals are incomplete')
            self._vectors([json.loads(r['embedding_json'])],1)
        return rows

    @staticmethod
    def _result(correlation, generation, chunks, idempotent):
        return QueryResult(status='ok',trace_id=correlation.trace_id,data={'index':{
            'generation':generation,'chunk_count':chunks,'status':'ready','active':True,
            'activation_allowed':False,'idempotent':idempotent}})

    def _before_commit(self, db):
        """Fault-injection seam for transaction recovery tests."""

    async def rebuild(self, principal, correlation, request):
        try:
            if not all(isinstance(value,str) and _ID.fullmatch(value) for value in
                (request.space_id,request.collection_id,request.collection_version,request.capability,request.provider_id,request.idempotency_key)):
                raise ValueError('Invalid index request')
            scopes=set(principal.scopes)
            if (principal.tenant_id is not None or not {'knowledge.admin','knowledge:admin'} & scopes
                or not {f'knowledge.space:{request.space_id}',f'knowledge:space:{request.space_id}'} & scopes
                or request.space_id not in self.config['space_ids']):
                return QueryResult(status='error',trace_id=correlation.trace_id,error=QueryError(
                    code=QueryErrorCode.PERMISSION_DENIED,message='Index requires configured Space admin scope'))
            if request.provider_id!=self.provider_id or request.capability not in _CAPS:
                raise ValueError('Index provider is not enabled')
            async with self._lock:
                return await self._rebuild(principal,correlation,request)
        except Exception:
            return QueryResult(status='error',trace_id=correlation.trace_id,error=QueryError(
                code=QueryErrorCode.INDEX_NOT_READY,message='Local index rebuild failed verification or publication'))

    async def _rebuild(self, principal, correlation, request):
        key=(request.space_id,request.collection_id,request.collection_version,request.capability)
        signature,assets,collection=self._snapshot(key)
        request_fp=_digest(_json([key,signature,self.provider_id]).encode())
        scope_json=_json([request.space_id,principal.tenant_id])
        with self._db(True) as db:
            db.execute('BEGIN')
            prior=db.execute('SELECT * FROM knowledge_local_vector_requests WHERE idempotency_key=? AND subject_id=?',(request.idempotency_key,principal.subject_id)).fetchone()
            if prior:
                if prior['fingerprint']!=request_fp or prior['scope_json']!=scope_json:raise ValueError('Index request identity changed')
                row=db.execute('SELECT * FROM knowledge_local_vector_indexes WHERE space_id=? AND collection_id=? AND collection_version=? AND capability=? AND generation=?',(*key,prior['generation'])).fetchone()
                chunks=self._check_index(db,row,signature)
                if collection.get('provider_bindings',{}).get(request.capability)!={'provider_id':self.provider_id}:
                    raise ValueError('Index binding changed')
                await self._verify_sources([a for a in assets if a['kind'] in {'document','wiki_page'}], principal, request.space_id)
                return self._result(correlation,prior['generation'],len(chunks),True)
        texts=[]; total=0
        for asset in assets:
            if asset['kind'] not in {'document','wiki_page'}:continue
            if not str(asset.get('mime_type') or '').startswith('text/'):
                raise ValueError('Index requires normalized text Assets')
            req=BlobReadRequest(asset['source_uri'],principal,correlation,0,MAX_BLOB_READ_BYTES,asset['content_digest'])
            result=self.reader.read(req) if hasattr(self.reader,'read') else self.reader(req)
            if inspect.isawaitable(result):result=await result
            validate_blob_read_result(req,result)
            if _digest(result.content)!=asset['content_digest']:raise ValueError('Source content digest changed')
            total+=len(result.content)
            if total>_MAX_TOTAL_BYTES:raise ValueError('Index source budget exceeded')
            body=result.content.decode('utf-8')
            for offset in range(0,len(body),self.max_chars):
                text=body[offset:offset+self.max_chars].strip()
                if text:texts.append((asset,text))
                if len(texts)>_MAX_CHUNKS:raise ValueError('Index chunk budget exceeded')
        if not texts:raise ValueError('Index has no text chunks')
        if len(texts) * self.dimension > _MAX_COMPONENTS:
            raise ValueError('Index vector budget exceeded')
        vectors=[]
        for offset in range(0,len(texts),self.batch_size):
            batch=texts[offset:offset+self.batch_size]
            raw=await asyncio.to_thread(self.embedder.embed,[text for _,text in batch])
            vectors.extend(self._vectors(raw,len(batch)))
        await self._verify_sources([a for a in assets if a['kind'] in {'document','wiki_page'}], principal, request.space_id)
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            if self._snapshot(key)[0]!=signature:raise ValueError('Index source changed during rebuild')
            # A second process cannot silently replace the same request. The
            # owned runtime already holds the workspace lock for its lifetime.
            if db.execute('SELECT 1 FROM knowledge_local_vector_requests WHERE idempotency_key=? AND subject_id=?',(request.idempotency_key,principal.subject_id)).fetchone():
                raise ValueError('Concurrent index request publication')
            generation=1+db.execute('SELECT COALESCE(MAX(generation),0) FROM knowledge_local_vector_indexes WHERE space_id=? AND collection_id=? AND collection_version=? AND capability=?',key).fetchone()[0]
            rows=[]
            for ordinal,((asset,text),vector) in enumerate(zip(texts,vectors)):
                chunk_id='vector_'+hashlib.sha256(_json([key,generation,asset['id'],ordinal]).encode()).hexdigest()[:32]
                CitationCandidate(asset['id'],asset['source_uri'],quote=text[:1200],locator={'chunk_id':chunk_id})
                rows.append((*key,generation,ordinal,asset['id'],chunk_id,text,asset['content_digest'],asset['source_uri'],_json(vector)))
            if sum(len(row[8].encode('utf-8')) + len(row[11].encode('utf-8')) for row in rows) > _MAX_TOTAL_BYTES:
                raise ValueError('Serialized index exceeds verification budget')
            db.execute("UPDATE knowledge_local_vector_indexes SET status='inactive' WHERE space_id=? AND collection_id=? AND collection_version=? AND capability=?",key)
            db.execute('INSERT INTO knowledge_local_vector_indexes VALUES(?,?,?,?,?,?,?,?,?,?,?)',(*key,generation,signature,self.model_identity,self.dimension,len(rows),_digest(_json(rows).encode()),'active'))
            db.executemany('INSERT INTO knowledge_local_vector_chunks VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',rows)
            db.execute('INSERT INTO knowledge_local_vector_requests VALUES(?,?,?,?,?)',(request.idempotency_key,principal.subject_id,scope_json,request_fp,generation))
            self._before_commit(db)
            db.commit()
        return self._result(correlation,generation,len(rows),False)

    async def _verify_sources(self, assets, principal, space_id):
        principal = principal or Principal('knowledge-vector-verification',
            scopes=('knowledge.read', f'knowledge.space:{space_id}'))
        total = 0
        try:
            for asset in assets:
                request = BlobReadRequest(asset['source_uri'], principal, Correlation('vector-source-check'),
                    0, MAX_BLOB_READ_BYTES, asset['content_digest'])
                result = self.reader.read(request) if hasattr(self.reader, 'read') else self.reader(request)
                if inspect.isawaitable(result): result = await result
                validate_blob_read_result(request, result)
                total += len(result.content)
                if total > _MAX_TOTAL_BYTES or _digest(result.content) != asset['content_digest']:
                    raise ValueError('Vector source changed or exceeds query budget')
        except Exception as error:
            raise RetrievalIndexNotReady('Current vector source is unavailable') from error

    async def search(self, *, query, space_id, limit, collection_id=None, collection_version=None, capability='document_rag_query', principal=None):
        if space_id not in self.config['space_ids'] or capability not in _CAPS:
            return ()
        try:
            with self._db(True) as db:
                db.execute('BEGIN')
                indexes=db.execute("SELECT * FROM knowledge_local_vector_indexes WHERE space_id=? AND capability=? AND status='active' AND (? IS NULL OR collection_id=?) AND (? IS NULL OR collection_version=?)",(space_id,capability,collection_id,collection_id,collection_version,collection_version)).fetchall()
                current_keys = {(c['id'], c['version']) for c in self.repository.list_collections(space_id=space_id)}
                indexes = [row for row in indexes if (row['collection_id'], row['collection_version']) in current_keys]
                if not indexes:
                    if collection_id is not None:raise RetrievalIndexNotReady('Local index is unavailable')
                    return ()
                if sum(row['chunk_count'] * self.dimension for row in indexes) > _MAX_COMPONENTS:
                    raise RetrievalIndexNotReady('Query vector budget exceeded')
                candidates=[]; snapshots=[]; source_assets={}
                for row in indexes:
                    key=tuple(row[k] for k in ('space_id','collection_id','collection_version','capability'))
                    signature,assets,collection=self._snapshot(key)
                    if collection.get('provider_bindings',{}).get(capability)!={'provider_id':self.provider_id}:
                        if collection_id is not None:raise RetrievalIndexNotReady('Local index binding changed')
                        continue
                    rows=self._check_index(db,row,signature)
                    source_assets.update({a["id"]:a for a in assets if a["kind"] in {"document","wiki_page"}})
                    candidates.extend(rows);snapshots.append((key,signature,row['generation']))
            if not candidates:return ()
            await self._verify_sources(source_assets.values(), principal, space_id)
            raw=await asyncio.to_thread(self.embedder.embed,[query]);q=self._vectors(raw,1)[0]
            await self._verify_sources(source_assets.values(), principal, space_id)
            # The model request is outside the read transaction; recheck the
            # current publication before returning any evidence.
            with self._db(True) as db:
                for key,signature,generation in snapshots:
                    current,_,collection=self._snapshot(key)
                    row=db.execute('SELECT * FROM knowledge_local_vector_indexes WHERE space_id=? AND collection_id=? AND collection_version=? AND capability=? AND generation=?',(*key,generation)).fetchone()
                    self._check_index(db,row,current)
                    if current!=signature or collection.get('provider_bindings',{}).get(capability)!={'provider_id':self.provider_id}:
                        raise RetrievalIndexNotReady('Local index changed during query')
            results=[]
            for item in candidates:
                vector=self._vectors([json.loads(item['embedding_json'])],1)[0]
                score=max(0.0,min(1.0,sum(a*b for a,b in zip(q,vector))))
                results.append(CitationCandidate(item['asset_id'],item['resource_uri'],quote=item['text'][:1200],locator={'chunk_id':item['chunk_id']},score=score))
            results.sort(key=lambda item:(-(item.score or 0),item.asset_id,item.locator['chunk_id']))
            return tuple(results[:limit])
        except RetrievalIndexNotReady:raise
        except Exception as error:
            raise RetrievalProviderError('Local vector query is unavailable') from error
