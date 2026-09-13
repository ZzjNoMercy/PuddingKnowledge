import json,sqlite3
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from knowledge_contracts import Principal
from knowledge_platform.local.wiki_authoring import WikiAuthoringStore
from knowledge_platform.local.wiki_authoring_projection import asset_id,collection_id,AuthoringReaderServices
from knowledge_platform.local.wiki_query import PublishedWikiReader
from knowledge_platform.local.app import _build_app
from knowledge_platform.catalog.sqlite_query import SqliteCatalogQueryRepository
from knowledge_platform.local.workspace import open_persistent_workspace
from knowledge_platform.wiki.patch import WikiPatch,PageChange,digest
from test_wiki_schema_publication import prepare,markdown


def setup(tmp_path):
    owned,state,config,request=prepare(tmp_path);store=WikiAuthoringStore.from_owned_workspace(owned)
    revision=store.read()[0]
    body=markdown('UniqueAlpha')
    patch=WikiPatch(revision,(PageChange('concepts/a',body,None),),('source.md',),'[[concepts/a]]','Added A.')
    store.apply(patch,operation_id='add')
    return owned,state,store,body


def client(owned,store):
    repo=SqliteCatalogQueryRepository(owned['catalog']);reader=PublishedWikiReader(repo,AuthoringReaderServices(store))
    principal=Principal(subject_id='test',scopes=('knowledge.query','knowledge.read','knowledge.search',f'knowledge.space:{store.space_id}'))
    return TestClient(_build_app(repo,owned['file_bindings'],principal,wiki_provider=reader,wiki_blob_reader=reader))


def test_current_collection_query_update_retire_and_restart(tmp_path):
    owned,state,store,body=setup(tmp_path);uri=f'knowledge://spaces/{store.space_id}/assets/{asset_id(store.space_id,"concepts/a")}'
    api=client(owned,store);cid=collection_id(store.space_id)
    response=api.post('/v1/query',json={'query':'UniqueAlpha','space_id':store.space_id,'collection_id':cid})
    assert response.status_code==200 and response.json()['status']=='ok',response.text
    assert uri in response.text
    rev,pages,index,log=store.read()
    store.apply(WikiPatch(rev,(PageChange('concepts/a',markdown('UniqueBeta'),digest(body)),),('source.md',),index,'Updated.'),operation_id='update')
    assert store.read_published(uri)==markdown('UniqueBeta').encode()
    assert uri in api.post('/v1/query',json={'query':'UniqueBeta','space_id':store.space_id,'collection_id':cid}).text
    rev,pages,index,log=store.read()
    store.apply(WikiPatch(rev,(PageChange('concepts/a',None,digest(pages['concepts/a'])),),(), '# Empty index','Retired.'),operation_id='retire')
    with pytest.raises(LookupError):store.read_published(uri)
    with sqlite3.connect(store.database) as db:
        assert db.execute('SELECT id FROM knowledge_assets WHERE id=?',(asset_id(store.space_id,'concepts/a'),)).fetchone() is None
        assert json.loads(db.execute('SELECT asset_ids FROM knowledge_datasets WHERE id=?',(cid,)).fetchone()[0])==[]
    with open_persistent_workspace(state) as restarted:pass
    assert WikiAuthoringStore.from_owned_workspace(restarted).read()[1]=={}


@pytest.mark.parametrize('attack',['delete_asset','digest','collection','disable'])
def test_current_reader_rejects_projection_corruption(tmp_path,attack):
    owned,state,store,body=setup(tmp_path)
    with sqlite3.connect(store.database) as db:
        if attack=='delete_asset':db.execute("DELETE FROM knowledge_assets WHERE source_type='local_wiki_authoring'")
        elif attack=='digest':db.execute("UPDATE knowledge_assets SET content_digest='sha256:bad' WHERE source_type='local_wiki_authoring'")
        elif attack=='collection':db.execute('UPDATE knowledge_datasets SET asset_ids=? WHERE id=?',('[]',collection_id(store.space_id)))
        else:db.execute('UPDATE knowledge_wiki_authoring_state SET catalog_projected=0')
    with pytest.raises(ValueError):store.read_published(f'knowledge://spaces/{store.space_id}/assets/{asset_id(store.space_id,"concepts/a")}')


def test_projection_failure_rolls_back_authoring_and_catalog(tmp_path,monkeypatch):
    from knowledge_platform.local import wiki_authoring_projection as projection
    owned,state,store,body=setup(tmp_path);before=store.read();original=projection.project
    def fail(*args,**kwargs):original(*args,**kwargs);raise ValueError('after projection')
    monkeypatch.setattr(projection,'project',fail)
    with pytest.raises(ValueError,match='after projection'):
        store.apply(WikiPatch(before[0],(PageChange('concepts/a',markdown('Different'),digest(body)),),('source.md',),before[2],'Update.'),operation_id='fail')
    assert store.read()==before


def test_legacy_unprojected_state_upgrades_explicitly(tmp_path):
    owned,state,config,request=prepare(tmp_path)
    # Recreate the prior four-column state shape before opening the owned adapter.
    store=WikiAuthoringStore(database=owned['catalog'],space_id=owned['space_id'],bundle=owned['schema_bundle'],raw_hashes={'source.md':request.content_digest[7:]},raw_manifest_sha256=digest((owned['evidence_root']/'archive/raw/manifest.jsonl').read_text()))
    store.initialize(pages={},index='# Index',log='# Log')
    with sqlite3.connect(store.database) as db:db.execute('ALTER TABLE knowledge_wiki_authoring_state DROP COLUMN catalog_projected')
    upgraded=WikiAuthoringStore.from_owned_workspace(owned)
    assert upgraded.catalog_projection and upgraded.read()[1]=={}


def test_search_snapshot_remains_consistent_across_wal_commit(tmp_path,monkeypatch):
    owned,state,store,body=setup(tmp_path);before=store.read()
    with sqlite3.connect(store.database) as db:db.execute('PRAGMA journal_mode=WAL')
    other=WikiAuthoringStore(database=store.database,space_id=store.space_id,bundle=store.bundle,raw_hashes=store.raw,raw_manifest_sha256=store.raw_manifest,catalog_projection=True)
    request=WikiPatch(before[0],(PageChange('concepts/a',markdown('UniqueBeta'),digest(body)),),('source.md',),before[2],'Concurrent update.')
    original=store._read;written=[]
    def after_read(db,**kwargs):
        result=original(db,**kwargs)
        if not written:
            written.append(True);other.apply(request,operation_id='concurrent')
        return result
    monkeypatch.setattr(store,'_read',after_read)
    snapshot=AuthoringReaderServices(store).search_snapshot(store.space_id)
    assert len(snapshot)==1 and snapshot[0][1]==body.encode()
    assert snapshot[0][0]['content_digest']=='sha256:'+digest(body)
    assert store.read()[1]['concepts/a']==markdown('UniqueBeta')


def test_asset_identity_collision_does_not_overwrite_foreign_asset(tmp_path):
    owned,state,config,request=prepare(tmp_path);store=WikiAuthoringStore.from_owned_workspace(owned);before=store.read()
    identifier=asset_id(store.space_id,'concepts/a')
    with sqlite3.connect(store.database) as db:
        db.execute("INSERT INTO knowledge_assets (id,space_id,kind,title,description,mime_type,source_type,source_uri,revision,content_digest,permissions_json,metadata_json,created_at,updated_at) VALUES (?,?,'document','Foreign','','text/plain','foreign','foreign://x','r','d','{}','{}','now','now')",(identifier,store.space_id))
    with pytest.raises(ValueError,match='identity collision'):
        store.apply(WikiPatch(before[0],(PageChange('concepts/a',markdown(),None),),('source.md',),'[[concepts/a]]','Add.'),operation_id='collision')
    assert store.read()==before
    with sqlite3.connect(store.database) as db:assert db.execute('SELECT title FROM knowledge_assets WHERE id=?',(identifier,)).fetchone()[0]=='Foreign'
