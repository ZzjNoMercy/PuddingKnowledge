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
    def __init__(self, catalog_path, repository, reader, embedder, config, storage=None, reranker=None):
        self.catalog_path, self.repository, self.reader, self.embedder = catalog_path, repository, reader, embedder
        self.config = json.loads(_json(config))
        self.dimension = self.config['embedding']['dimension']
        self.provider_id = self.config['provider_id']
        self.batch_size, self.max_chars = self.config['batch_size'], self.config['max_chars']
        if self.provider_id not in {'knowledge_local_vector','knowledge_milvus_vector'} or type(self.dimension) is not int or not 1 <= self.dimension <= 16384:
            raise ValueError('Invalid vector provider configuration')
        if self.provider_id == 'knowledge_milvus_vector' and storage is None: raise ValueError('Milvus storage is required')
        if self.provider_id == 'knowledge_local_vector' and storage is not None:
            raise ValueError('Local vector provider cannot own remote storage')
        self.storage = storage
        from knowledge_platform.local.index_config import validate_retrieval
        self.retrieval=validate_retrieval(self.config['retrieval']) if 'retrieval' in self.config else None
        self.reranker=reranker
        if (reranker is not None)!=(self.retrieval is not None and self.retrieval['rerank'] is not None):
            raise ValueError('Reranker must match explicit retrieval configuration')
        if type(self.batch_size) is not int or not 1 <= self.batch_size <= 256 or type(self.max_chars) is not int or not 100 <= self.max_chars <= 12000:
            raise ValueError('Invalid vector chunk configuration')
        embedding = self.config['embedding']
        if embedding.get('protocol','openai') not in {'openai','dashscope_multimodal'}:
            raise ValueError('Embedding protocol is invalid')
        self.multimodal=embedding.get('protocol','openai')=='dashscope_multimodal'
        model_identity={k:embedding[k] for k in ('endpoint','model','dimension')}
        if self.multimodal:model_identity['protocol']='dashscope_multimodal'
        self.model_identity = _digest(_json(model_identity).encode())
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
            columns = {row[1] for row in db.execute('PRAGMA table_info(knowledge_local_vector_indexes)')}
            if 'provider_id' not in columns: db.execute("ALTER TABLE knowledge_local_vector_indexes ADD COLUMN provider_id TEXT NOT NULL DEFAULT 'knowledge_local_vector'")
            db.execute('CREATE TABLE IF NOT EXISTS knowledge_vector_locations (space_id TEXT, collection_id TEXT, collection_version TEXT, capability TEXT, generation INTEGER, storage_identity TEXT, collection_name TEXT, PRIMARY KEY(space_id,collection_id,collection_version,capability,generation))')

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
            fact={k:asset.get(k) for k in ('id','space_id','kind','source_type','source_uri','revision','content_digest','mime_type')}
            if self.multimodal and self._is_image(asset):
                fact.update(title=asset.get('title',''),description=asset.get('description',''))
            assets.append(fact)
        facts = {k:collection.get(k) for k in ('id','space_id','version','name','kind','asset_ids','semantic_asset_ids','capabilities')}
        signature = _digest(_json({'collection':facts,'assets':assets,'capability':capability,
            'model_identity':self.model_identity,'max_chars':self.max_chars}).encode())
        return signature, assets, collection

    @staticmethod
    def _is_image(asset):
        return asset.get('kind') in {'image','derived_media','original_file'} and str(asset.get('mime_type') or '').startswith('image/')

    def _indexed_asset(self,asset):
        return asset['kind'] in {'document','wiki_page'} or (self.multimodal and self._is_image(asset))

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
        if row is None or row['status']!='active' or row['provider_id']!=self.provider_id or row['fingerprint']!=signature or row['model_identity']!=self.model_identity or row['dimension']!=self.dimension:
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
        storage_identity = str(self.storage.identity) if self.provider_id == 'knowledge_milvus_vector' else ''
        request_identity=[key,signature,self.provider_id]
        if self.storage is not None: request_identity.append(storage_identity)
        request_fp=_digest(_json(request_identity).encode())
        scope_json=_json([request.space_id,principal.tenant_id])
        prior_snapshot = None
        with self._db(True) as db:
            db.execute('BEGIN')
            prior=db.execute('SELECT * FROM knowledge_local_vector_requests WHERE idempotency_key=? AND subject_id=?',(request.idempotency_key,principal.subject_id)).fetchone()
            if prior:
                if prior['fingerprint']!=request_fp or prior['scope_json']!=scope_json:raise ValueError('Index request identity changed')
                row=db.execute('SELECT * FROM knowledge_local_vector_indexes WHERE space_id=? AND collection_id=? AND collection_version=? AND capability=? AND generation=?',(*key,prior['generation'])).fetchone()
                chunks=self._check_index(db,row,signature)
                if collection.get('provider_bindings',{}).get(request.capability)!={'provider_id':self.provider_id}:
                    raise ValueError('Index binding changed')
                location=self._location(db,key,prior['generation'])
                prior_snapshot=(key,signature,prior['generation'],location)
        if prior_snapshot is not None:
            if self.storage is not None:
                await asyncio.to_thread(self.storage.verify,location[1],
                    [json.loads(chunk['embedding_json']) for chunk in chunks])
            await self._verify_sources([a for a in assets if self._indexed_asset(a)],principal,request.space_id)
            self._verify_publications([prior_snapshot])
            return self._result(correlation,prior['generation'],len(chunks),True)
        texts=[]; total=0;image_payloads={}
        for asset in assets:
            if not self._indexed_asset(asset):continue
            is_image=self.multimodal and self._is_image(asset)
            if not is_image and not str(asset.get('mime_type') or '').startswith('text/'):
                raise ValueError('Index requires normalized text Assets')
            req=BlobReadRequest(asset['source_uri'],principal,correlation,0,MAX_BLOB_READ_BYTES,asset['content_digest'])
            result=self.reader.read(req) if hasattr(self.reader,'read') else self.reader(req)
            if inspect.isawaitable(result):result=await result
            validate_blob_read_result(req,result)
            if _digest(result.content)!=asset['content_digest']:raise ValueError('Source content digest changed')
            total+=len(result.content)
            if total>_MAX_TOTAL_BYTES:raise ValueError('Index source budget exceeded')
            if is_image:
                label=('Image: '+str(asset.get('title') or '')+' '+str(asset.get('description') or '')).strip()[:1200]
                CitationCandidate(asset['id'],asset['source_uri'],quote=label)
                texts.append((asset,label))
                image_payloads[asset['id']]=(result.content,asset['mime_type'])
                if len(texts)>_MAX_CHUNKS:raise ValueError('Index chunk budget exceeded')
                continue
            body=result.content.decode('utf-8')
            for offset in range(0,len(body),self.max_chars):
                text=body[offset:offset+self.max_chars].strip()
                if text:texts.append((asset,text))
                if len(texts)>_MAX_CHUNKS:raise ValueError('Index chunk budget exceeded')
        if not texts:raise ValueError('Index has no supported content')
        if len(texts) * self.dimension > _MAX_COMPONENTS:
            raise ValueError('Index vector budget exceeded')
        vectors=[None]*len(texts)
        for offset in range(0,len(texts),self.batch_size):
            positions=range(offset,min(offset+self.batch_size,len(texts)))
            text_positions=[i for i in positions if texts[i][0]['id'] not in image_payloads]
            image_positions=[i for i in positions if texts[i][0]['id'] in image_payloads]
            if text_positions:
                raw=await asyncio.to_thread(self.embedder.embed,[texts[i][1] for i in text_positions])
                for i,vector in zip(text_positions,self._vectors(raw,len(text_positions))):vectors[i]=vector
            if image_positions:
                raw=await asyncio.to_thread(self.embedder.embed_images,[image_payloads[texts[i][0]['id']] for i in image_positions])
                for i,vector in zip(image_positions,self._vectors(raw,len(image_positions))):vectors[i]=vector
        await self._verify_sources([a for a in assets if self._indexed_asset(a)], principal, request.space_id)
        remote_name = None
        if self.provider_id == 'knowledge_milvus_vector':
            remote_name = await asyncio.to_thread(self.storage.prepare, vectors, identity=signature)
            await asyncio.to_thread(self.storage.verify, remote_name, vectors)
            await self._verify_sources([a for a in assets if self._indexed_asset(a)], principal, request.space_id)
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
            db.execute('INSERT INTO knowledge_local_vector_indexes (space_id,collection_id,collection_version,capability,generation,fingerprint,model_identity,dimension,chunk_count,chunks_digest,status,provider_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(*key,generation,signature,self.model_identity,self.dimension,len(rows),_digest(_json(rows).encode()),'active',self.provider_id))
            db.executemany('INSERT INTO knowledge_local_vector_chunks VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',rows)
            db.execute('INSERT INTO knowledge_local_vector_requests VALUES(?,?,?,?,?)',(request.idempotency_key,principal.subject_id,scope_json,request_fp,generation))
            if remote_name is not None:
                db.execute('INSERT INTO knowledge_vector_locations VALUES(?,?,?,?,?,?,?)',(*key,generation,str(self.storage.identity),remote_name))
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

    def _location(self, db, key, generation):
        if self.storage is None:return None
        row=db.execute('SELECT storage_identity,collection_name FROM knowledge_vector_locations WHERE space_id=? AND collection_id=? AND collection_version=? AND capability=? AND generation=?',(*key,generation)).fetchone()
        if row is None or row['storage_identity']!=self.storage.identity:
            raise RetrievalIndexNotReady('Remote vector location is unavailable')
        return (row['storage_identity'],row['collection_name'])

    def _verify_publications(self, snapshots):
        with self._db(True) as db:
            db.execute('BEGIN')
            for key,signature,generation,location in snapshots:
                current,_,collection=self._snapshot(key)
                row=db.execute('SELECT * FROM knowledge_local_vector_indexes WHERE space_id=? AND collection_id=? AND collection_version=? AND capability=? AND generation=?',(*key,generation)).fetchone()
                self._check_index(db,row,current)
                if (current!=signature or collection.get('provider_bindings',{}).get(key[3])!={'provider_id':self.provider_id}
                    or self._location(db,key,generation)!=location):
                    raise RetrievalIndexNotReady('Vector publication changed during request')

    async def search(self, *, query, space_id, limit, collection_id=None, collection_version=None, capability='document_rag_query', principal=None):
        if space_id not in self.config['space_ids'] or capability not in _CAPS:
            return ()
        try:
            if not isinstance(query,str) or not query.strip() or len(query)>512 or type(limit) is not int or not 1<=limit<=50:
                raise ValueError('Invalid vector query')
            with self._db(True) as db:
                db.execute('BEGIN')
                indexes=db.execute("SELECT * FROM knowledge_local_vector_indexes WHERE space_id=? AND capability=? AND status='active' AND (? IS NULL OR collection_id=?) AND (? IS NULL OR collection_version=?)",(space_id,capability,collection_id,collection_id,collection_version,collection_version)).fetchall()
                current_keys = {(c['id'], c['version']) for c in self.repository.list_collections(space_id=space_id)}
                indexes = [row for row in indexes if (row['collection_id'], row['collection_version']) in current_keys]
                if not indexes:
                    if collection_id is not None:raise RetrievalIndexNotReady('Vector index is unavailable')
                    return ()
                if sum(row['chunk_count'] * self.dimension for row in indexes) > _MAX_COMPONENTS:
                    raise RetrievalIndexNotReady('Query vector budget exceeded')
                groups=[]; snapshots=[]; source_assets={}
                for row in indexes:
                    key=tuple(row[k] for k in ('space_id','collection_id','collection_version','capability'))
                    signature,assets,collection=self._snapshot(key)
                    if collection.get('provider_bindings',{}).get(capability)!={'provider_id':self.provider_id}:
                        if collection_id is not None:raise RetrievalIndexNotReady('Vector index binding changed')
                        continue
                    rows=self._check_index(db,row,signature)
                    source_assets.update({a['id']:a for a in assets if self._indexed_asset(a)})
                    location=self._location(db,key,row['generation'])
                    groups.append((rows,location));snapshots.append((key,signature,row['generation'],location))
            if not groups:return ()
            await self._verify_sources(source_assets.values(), principal, space_id)
            dense_enabled=self.retrieval is None or self.retrieval['vector_weight']>0
            q=None
            if dense_enabled:
                raw=await asyncio.to_thread(self.embedder.embed,[query]);q=self._vectors(raw,1)[0]
            candidate_limit=max(limit,self.retrieval['candidate_limit']) if self.retrieval is not None else limit
            candidates=[]
            for rows,location in groups:
                if not dense_enabled:continue
                if self.storage is None:
                    candidates.extend(rows)
                    continue
                vectors=[json.loads(row['embedding_json']) for row in rows]
                await asyncio.to_thread(self.storage.verify,location[1],vectors)
                hits=await asyncio.to_thread(self.storage.search,location[1],q,candidate_limit)
                if not isinstance(hits,(list,tuple)) or len(hits)!=min(candidate_limit,len(rows)):
                    raise RetrievalIndexNotReady('Remote vector results are incomplete')
                seen=set()
                for hit in hits:
                    if not isinstance(hit,(list,tuple)) or len(hit)!=2:
                        raise RetrievalIndexNotReady('Remote vector result is invalid')
                    ordinal,score=hit
                    if (type(ordinal) is not int or not 0<=ordinal<len(rows) or ordinal in seen
                        or type(score) not in (int,float) or not math.isfinite(score)):
                        raise RetrievalIndexNotReady('Remote vector ordinal is invalid')
                    seen.add(ordinal);candidates.append(rows[ordinal])
            # Rank only checked local chunks. The remote channels cannot
            # supply text, resource URIs, collection identity, or new candidates.
            all_rows=sorted([row for rows,_ in groups for row in rows],
                key=lambda row:(row['asset_id'],row['chunk_id']))
            positions={row['chunk_id']:i for i,row in enumerate(all_rows)}
            dense=[]
            for item in candidates:
                vector=self._vectors([json.loads(item['embedding_json'])],1)[0]
                score=max(0.0,min(1.0,sum(a*b for a,b in zip(q,vector))))
                dense.append((positions[item['chunk_id']],score))
            dense.sort(key=lambda item:(-item[1],item[0]))
            ranked=dense[:candidate_limit]
            if self.retrieval is not None:
                if len(all_rows)>10000 or sum(len(row['text'].encode('utf-8')) for row in all_rows)>_MAX_TOTAL_BYTES:
                    raise RetrievalIndexNotReady('Hybrid corpus exceeds verification bound')
                from knowledge_platform.retrieval.hybrid import bm25_rank,rrf_fuse
                lexical=(await asyncio.to_thread(bm25_rank,query,[row['text'] for row in all_rows],candidate_limit)
                    if self.retrieval['bm25_weight']>0 else [])
                ranked=rrf_fuse([[i for i,_ in ranked],[i for i,_ in lexical]],
                    [self.retrieval['vector_weight'],self.retrieval['bm25_weight']],self.retrieval['rrf_k'],candidate_limit)
                if self.reranker is not None and ranked:
                    pool=[all_rows[i] for i,_ in ranked]
                    reranked=await asyncio.to_thread(self.reranker.rerank,query,[row['text'] for row in pool],min(limit,len(pool)))
                    if not isinstance(reranked,(list,tuple)) or len(reranked)!=min(limit,len(pool)):
                        raise RetrievalIndexNotReady('Reranker returned incomplete results')
                    seen=set();ordered=[]
                    for item in reranked:
                        if not isinstance(item,(list,tuple)) or len(item)!=2:
                            raise RetrievalIndexNotReady('Reranker returned invalid results')
                        ordinal,score=item
                        if (type(ordinal) is not int or not 0<=ordinal<len(pool) or ordinal in seen
                            or type(score) not in (int,float) or not math.isfinite(score) or not 0<=score<=1):
                            raise RetrievalIndexNotReady('Reranker returned invalid identity or score')
                        seen.add(ordinal);ordered.append((ranked[ordinal][0],float(score)))
                    ranked=sorted(ordered,key=lambda item:(-item[1],item[0]))
            # Verify again after fusion/rerank, which may await model HTTP.
            await self._verify_sources(source_assets.values(), principal, space_id)
            self._verify_publications(snapshots)
            results=[]
            for ordinal,score in ranked[:limit]:
                item=all_rows[ordinal]
                results.append(CitationCandidate(item['asset_id'],item['resource_uri'],quote='' if self.multimodal and self._is_image(source_assets[item['asset_id']]) else item['text'][:1200],locator={'chunk_id':item['chunk_id']},score=score))
            return tuple(results)
        except RetrievalIndexNotReady:raise
        except Exception as error:
            raise RetrievalProviderError('Vector query is unavailable') from error
