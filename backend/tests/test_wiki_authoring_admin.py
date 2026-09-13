import json,sqlite3
import pytest
from fastapi.testclient import TestClient
from knowledge_contracts import Principal
from knowledge_platform.catalog import SqliteCatalogQueryRepository
from knowledge_platform.local.app import _build_app
from knowledge_platform.local.wiki_authoring import WikiAuthoringStore
from knowledge_platform.local.wiki_authoring_admin import WikiAuthoringAdmin
from knowledge_platform.wiki.patch import digest
from test_wiki_schema_publication import prepare,markdown


def setup(tmp_path,scopes=None,tenant=None):
    owned,state,_,_=prepare(tmp_path);store=WikiAuthoringStore.from_owned_workspace(owned)
    principal=Principal(subject_id='editor',scopes=tuple(scopes) if scopes is not None else ('knowledge.admin',f'knowledge.space:{store.space_id}'),tenant_id=tenant)
    service=WikiAuthoringAdmin(store,evidence_root=owned['evidence_root'])
    app=_build_app(SqliteCatalogQueryRepository(store.database),owned['file_bindings'],principal,wiki_authoring=service)
    return owned,store,TestClient(app)


def request(store):
    return {'space_id':store.space_id,'patch':{'expected_revision':store.read()[0],'changes':[
        {'slug':'concepts/a','markdown':markdown('Alpha [[concepts/b]]'),'expected_digest':None},
        {'slug':'concepts/b','markdown':markdown('Beta [[concepts/a]]'),'expected_digest':None}],
        'selected_raw':['source.md'],'index':'[[concepts/a]]\n[[concepts/b]]','log_entry':'Created two linked concepts.'}}


def test_admin_context_preview_apply_retry_update_retire(tmp_path):
    owned,store,api=setup(tmp_path);before=store.read()
    context=api.post('/v1/wiki/authoring/context',json={'space_id':store.space_id,'selected_raw':['source.md']}).json()
    assert context['status']=='ok' and context['data']['revision']==before[0]
    assert context['data']['raw'][0]['content']=='The source describes a concept.'
    assert str(tmp_path) not in json.dumps(context)
    body=request(store);preview=api.post('/v1/wiki/authoring/preview',json=body).json()
    assert preview['status']=='ok' and store.read()==before
    body['operation_id']='create';applied=api.post('/v1/wiki/authoring/apply',json=body).json()
    assert applied['status']=='ok' and applied['data']['revision']==preview['data']['revision']
    assert api.post('/v1/wiki/authoring/apply',json=body).json()['data']==applied['data']
    body['operation_id']='stale'
    assert api.post('/v1/wiki/authoring/apply',json=body).json()['status']=='error'
    current=store.read();body['patch']['expected_revision']=current[0]
    body['patch']['changes']=[{'slug':'concepts/a','markdown':markdown('Updated Alpha'),'expected_digest':digest(current[1]['concepts/a'])},{'slug':'concepts/b','markdown':None,'expected_digest':digest(current[1]['concepts/b']),'replacement':'concepts/a'}]
    body['patch']['index']='[[concepts/a]]';body['operation_id']='update-retire'
    assert api.post('/v1/wiki/authoring/apply',json=body).json()['status']=='ok'
    assert set(store.read()[1])=={'concepts/a'}
    context=api.post('/v1/wiki/authoring/context',json={'space_id':store.space_id,'slugs':['concepts/a']}).json()
    assert context['data']['pages']['concepts/a']==markdown('Updated Alpha')


@pytest.mark.parametrize('scopes,tenant',[([],None),(['knowledge.read'],None),(['knowledge.admin'],None),(['knowledge.admin','knowledge.space:*'],None),(['knowledge.admin'],'tenant')])
def test_permission_denied_before_store_access(tmp_path,scopes,tenant,monkeypatch):
    owned,store,api=setup(tmp_path,scopes,tenant)
    def forbidden(*args,**kwargs):raise AssertionError('unauthorized store access')
    monkeypatch.setattr(store,'read',forbidden)
    for action,body in [('context',{'space_id':store.space_id}),('preview',{'space_id':store.space_id,'patch':{}}),('apply',{'space_id':store.space_id,'operation_id':'x','patch':{}})]:
        result=api.post('/v1/wiki/authoring/'+action,json=body).json()
        assert result['error']['code']=='permission_denied'


@pytest.mark.parametrize('attack',['unknown','wrong-space','duplicate','path','nested','oversize','nonfinite'])
def test_bad_request_no_mutation(tmp_path,attack,monkeypatch):
    owned,store,api=setup(tmp_path);before=store.read();body={'space_id':store.space_id}
    if attack=='unknown':body['filesystem_root']='/tmp'
    if attack=='wrong-space':body['space_id']='space_other'
    if attack=='path':body['selected_raw']=['../../secret']
    content=json.dumps(body)
    if attack=='duplicate':content='{"space_id":"x","space_id":"y"}'
    if attack=='nested':content='['*40+'0'+']'*40
    if attack=='nonfinite':content='{"space_id":NaN}'
    if attack=='oversize':monkeypatch.setattr('knowledge_platform.transport.fastapi_wiki_authoring_router.MAX_REQUEST_BYTES',8)
    result=api.post('/v1/wiki/authoring/context',content=content,headers={'Content-Type':'application/json'}).json()
    assert result['status']=='error' and store.read()==before
    assert str(tmp_path) not in json.dumps(result)


def test_admin_is_not_exposed_by_default(tmp_path):
    owned,state,_,_=prepare(tmp_path)
    api=TestClient(_build_app(SqliteCatalogQueryRepository(owned['catalog']),owned['file_bindings'],Principal(subject_id='local',scopes=('knowledge.admin',))))
    assert api.post('/v1/wiki/authoring/context',json={}).status_code==404


def test_failed_final_lint_never_publishes(tmp_path):
    owned,store,api=setup(tmp_path);before=store.read();body=request(store)
    body['patch']['changes'][1]['markdown']=markdown('[[concepts/missing]]');body['operation_id']='broken'
    assert api.post('/v1/wiki/authoring/apply',json=body).json()['status']=='error'
    assert store.read()==before
    with sqlite3.connect(store.database) as db:assert db.execute('SELECT count(*) FROM knowledge_wiki_authoring_commits').fetchone()[0]==0


@pytest.mark.parametrize('attack',['bytes','symlink'])
def test_context_revalidates_owned_raw(tmp_path,attack):
    owned,store,api=setup(tmp_path)
    path=next(iter(owned['raw_bindings'].values()))
    if attack=='bytes':path.write_text('changed')
    else:
        path.unlink();path.symlink_to(tmp_path/'brain/raw/source.md')
    result=api.post('/v1/wiki/authoring/context',json={'space_id':store.space_id,'selected_raw':['source.md']}).json()
    assert result['status']=='error' and str(tmp_path) not in json.dumps(result)


def test_rejects_simple_browser_post_and_reused_operation(tmp_path):
    owned,store,api=setup(tmp_path);before=store.read()
    assert api.post('/v1/wiki/authoring/context',content=json.dumps({'space_id':store.space_id}),headers={'Content-Type':'text/plain'}).json()['status']=='error'
    body=request(store);body['operation_id']='same'
    assert api.post('/v1/wiki/authoring/apply',json=body).json()['status']=='ok'
    committed=store.read();body['patch']['log_entry']='Different intent'
    assert api.post('/v1/wiki/authoring/apply',json=body).json()['status']=='error'
    assert store.read()==committed


def test_context_returns_bounded_registered_raw_inventory(tmp_path):
    owned,store,api=setup(tmp_path)
    original_read=store.read()
    store.raw.update({
        'a.md': digest('a'), 'b.md': digest('b'), 'c.md': digest('c'),
    })
    # Keep the catalog commitment fixed while exercising inventory paging;
    # inventory itself must be sourced from the registered raw map only.
    store.read=lambda: original_read
    first=api.post('/v1/wiki/authoring/context',json={'space_id':store.space_id,'raw_limit':2}).json()
    assert first['status']=='ok'
    assert first['data']['raw_inventory']==[
        {'snapshot_path':'a.md','sha256':digest('a')},
        {'snapshot_path':'b.md','sha256':digest('b')},
    ]
    assert first['data']['raw_next_after']=='b.md'
    second=api.post('/v1/wiki/authoring/context',json={
        'space_id':store.space_id,'raw_after':first['data']['raw_next_after'],'raw_limit':2,
    }).json()
    assert second['data']['raw_inventory']==[{'snapshot_path':'c.md','sha256':digest('c')}, {'snapshot_path':'source.md','sha256':store.raw['source.md']}]
    assert second['data']['raw_next_after'] is None


@pytest.mark.parametrize('body', [
    {'raw_limit':0}, {'raw_limit':101}, {'raw_limit':True}, {'raw_after':1},
])
def test_context_rejects_invalid_raw_inventory_pagination(tmp_path,body):
    owned,store,api=setup(tmp_path)
    body={'space_id':store.space_id,**body}
    result=api.post('/v1/wiki/authoring/context',json=body).json()
    assert result['status']=='error'
