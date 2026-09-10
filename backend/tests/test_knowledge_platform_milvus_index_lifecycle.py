from __future__ import annotations
import asyncio, hashlib, sqlite3
from pathlib import Path
import pytest
from knowledge_contracts import BlobReadResult, Correlation, Principal
from knowledge_platform.catalog.index_rebuild import IndexRebuildRequest
from knowledge_platform.local.vector_index import LocalVectorIndex

class Repo:
    def __init__(self):
        body=b"hello milvus"; d="sha256:"+hashlib.sha256(body).hexdigest()
        self.body=body; self.collection={"id":"c","space_id":"s","version":"1","capabilities":["document_rag_query"],"asset_ids":["a"],"provider_bindings":{"document_rag_query":{"provider_id":"knowledge_milvus_vector"}}}; self.asset={"id":"a","space_id":"s","kind":"document","mime_type":"text/plain","source_uri":"knowledge://spaces/s/assets/a","content_digest":d}
    def list_collections(self, *, space_id): return [self.collection]
    def list_assets(self, *, space_id): return [self.asset]
class Reader:
    def __init__(self, repo): self.repo=repo
    async def read(self, req): return BlobReadResult(req.resource_uri,self.repo.body,req.expected_digest,0,len(self.repo.body),req.expected_digest)
class Embed:
    def embed(self, texts): return tuple((1.0,0.0) for _ in texts)
class Store:
    identity="milvus-test"
    def __init__(self): self.calls=[]; self.fail_verify=False; self.hits=[(0,1.0)]
    def prepare(self, vectors, *, identity): self.calls.append("prepare"); return "remote-random"
    def verify(self, name, vectors):
        self.calls.append("verify")
        if self.fail_verify: raise RuntimeError("remote unavailable")
    def search(self, name, vector, limit): self.calls.append("search"); return self.hits[:limit]
def setup(tmp_path):
    repo=Repo(); store=Store(); idx=LocalVectorIndex(tmp_path/"c.db",repo,Reader(repo),Embed(),{"version":1,"provider_id":"knowledge_milvus_vector","space_ids":["s"],"embedding":{"endpoint":"http://127.0.0.1:1","model":"m","dimension":2},"batch_size":4,"max_chars":1200},storage=store)
    with sqlite3.connect(tmp_path/"c.db") as db: db.execute("CREATE TABLE knowledge_collection_bindings (space_id TEXT,collection_id TEXT,collection_version TEXT,capability TEXT,binding_json TEXT,created_at TEXT,updated_at TEXT,PRIMARY KEY(space_id,collection_id,collection_version,capability))")
    return idx,repo,store,Principal("admin",scopes=("knowledge.admin","knowledge.space:s")),IndexRebuildRequest("s","c","1","document_rag_query","knowledge_milvus_vector","k")
def test_milvus_prepare_verify_location_and_search(tmp_path: Path):
    idx,repo,store,p,r=setup(tmp_path); result=asyncio.run(idx.rebuild(p,Correlation("t"),r)); assert result.status=="ok"; assert store.calls[:2]==["prepare","verify"]; assert asyncio.run(idx.search(query="hello",space_id="s",limit=2))[0].asset_id=="a"; assert "search" in store.calls

def test_remote_verify_failure_on_idempotent_replay_is_rejected(tmp_path: Path):
    idx,repo,store,p,r=setup(tmp_path); assert asyncio.run(idx.rebuild(p,Correlation("t"),r)).status=="ok"
    store.fail_verify=True
    replay=asyncio.run(idx.rebuild(p,Correlation("t2"),r))
    assert replay.status=="error"

@pytest.mark.parametrize("hits", [[(0,1.0),(0,.9)], [(1,1.0)], []])
def test_remote_duplicate_out_of_range_or_partial_ordinals_are_rejected(tmp_path: Path, hits):
    idx,repo,store,p,r=setup(tmp_path); assert asyncio.run(idx.rebuild(p,Correlation("t"),r)).status=="ok"; store.hits=hits
    with pytest.raises(Exception): asyncio.run(idx.search(query="hello",space_id="s",limit=5))


def test_multiple_collections_keep_ordinals_in_their_own_scope(tmp_path):
    idx,repo,store,p,r=setup(tmp_path)
    first=dict(repo.collection); second={**first,'id':'other','asset_ids':['b']}
    assets=[repo.asset,{**repo.asset,'id':'b','source_uri':'knowledge://spaces/s/assets/b'}]
    repo.list_collections=lambda **kw:[first,second]
    repo.list_assets=lambda **kw:assets
    counter=[]
    def prepare(vectors,*,identity):
        counter.append(vectors);return 'remote-'+str(len(counter))
    store.prepare=prepare
    assert asyncio.run(idx.rebuild(p,Correlation('a'),r)).status=='ok'
    other=IndexRebuildRequest('s','other','1','document_rag_query','knowledge_milvus_vector','other')
    assert asyncio.run(idx.rebuild(p,Correlation('b'),other)).status=='ok'
    result=asyncio.run(idx.search(query='hello',space_id='s',limit=5))
    assert {hit.asset_id for hit in result}=={'a','b'}
    assert len(result)==2


def test_source_revoked_during_remote_search_is_rejected(tmp_path):
    idx,repo,store,p,r=setup(tmp_path)
    assert asyncio.run(idx.rebuild(p,Correlation('a'),r)).status=='ok'
    def search(*args):
        repo.body=b'changed';return [(0,1.)]
    store.search=search
    with pytest.raises(Exception):asyncio.run(idx.search(query='hello',space_id='s',limit=1))


def test_source_revoked_during_prepare_does_not_publish(tmp_path):
    idx,repo,store,p,r=setup(tmp_path)
    def prepare(*args,**kwargs):
        repo.body=b'changed';return 'unreachable-new-remote'
    store.prepare=prepare
    assert asyncio.run(idx.rebuild(p,Correlation('a'),r)).status=='error'
    with sqlite3.connect(idx.catalog_path) as db:
        assert db.execute('SELECT COUNT(*) FROM knowledge_local_vector_indexes').fetchone()[0]==0


def test_remote_query_generation_change_does_not_return_old_evidence(tmp_path):
    idx,repo,store,p,r=setup(tmp_path)
    assert asyncio.run(idx.rebuild(p,Correlation('a'),r)).status=='ok'
    def search(*args):
        with sqlite3.connect(idx.catalog_path) as db:db.execute("UPDATE knowledge_local_vector_indexes SET status='inactive'")
        return [(0,1.)]
    store.search=search
    with pytest.raises(Exception):asyncio.run(idx.search(query='hello',space_id='s',limit=1))


def test_sql_rollback_leaves_previous_remote_generation_active(tmp_path):
    idx,repo,store,p,r=setup(tmp_path)
    assert asyncio.run(idx.rebuild(p,Correlation('a'),r)).status=='ok'
    def fail(db):raise RuntimeError('injected before commit')
    idx._before_commit=fail
    request=IndexRebuildRequest('s','c','1','document_rag_query','knowledge_milvus_vector','rollback')
    assert asyncio.run(idx.rebuild(p,Correlation('b'),request)).status=='error'
    assert asyncio.run(idx.search(query='hello',space_id='s',limit=1))[0].asset_id=='a'
    with sqlite3.connect(idx.catalog_path) as db:
        assert db.execute('SELECT COUNT(*) FROM knowledge_vector_locations').fetchone()[0]==1
        assert db.execute('SELECT COUNT(*) FROM knowledge_local_vector_requests').fetchone()[0]==1


def test_old_local_schema_and_request_fingerprint_survive_migration(tmp_path):
    from test_knowledge_platform_local_vector_index import _index
    idx,repo,embed,p,r=_index(tmp_path)
    assert asyncio.run(idx.rebuild(p,Correlation('old'),r)).status=='ok'
    with sqlite3.connect(idx.catalog_path) as db:db.execute('ALTER TABLE knowledge_local_vector_indexes DROP COLUMN provider_id')
    migrated=LocalVectorIndex(idx.catalog_path,repo,idx.reader,embed,idx.config)
    replay=asyncio.run(migrated.rebuild(p,Correlation('new'),r))
    assert replay.status=='ok' and replay.data['index']['idempotent']
