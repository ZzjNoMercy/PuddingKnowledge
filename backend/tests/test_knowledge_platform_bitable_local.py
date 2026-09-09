"""Local Bitable boundaries, with a real Catalog/Vault and adversarial provider."""
import asyncio
import copy
import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from knowledge_contracts import Principal
from knowledge_platform.catalog.models import KnowledgeAsset, KnowledgeConnector, KnowledgeSourceItem
from knowledge_platform.connector_sync.feishu_source import FeishuSource
from knowledge_platform.local.bitable import BitableError
from knowledge_platform.local.feishu import LocalFeishuService
from knowledge_platform.transport.fastapi_bitable_router import create_bitable_router
from test_knowledge_platform_feishu_composition import _fixture


class Api:
    def __init__(self):
        self.tables=[{'table_id':'tbl_1','name':'Cars'}, {'table_id':'tbl_2','name':'Owners'}]
        self.fields=[{'field_id':'fld_1','field_name':'Name','type':1}, {'field_id':'fld_2','field_name':'Secret','type':1}]
        self.calls=[]
        self.hook=None
        self.page={'items':[{'record_id':'rec_1','fields':{'Name':'ROW_CANARY_47021','Secret':'DROP_ME','Unexpected':'DROP_TOO'}}], 'has_more':True,'page_token':'provider_next','total':2}
    async def list_bitable_tables(self,**kwargs):return copy.deepcopy(self.tables)
    async def list_bitable_fields(self,**kwargs):return copy.deepcopy(self.fields)
    async def list_bitable_records_page(self,**kwargs):
        self.calls.append(kwargs)
        if self.hook:self.hook()
        return copy.deepcopy(self.page)


@pytest.fixture
def fixture(tmp_path,monkeypatch):
    catalog,engine,config=_fixture(tmp_path)
    config['sources'][0]['selection']={'kind':'bitable','root':'app_1','wiki_space':''}
    config['sources'][0]['bitable']={'tables':[{'table_id':'tbl_1','view_id':'view_1'}],'relations':[]}
    monkeypatch.setenv('FEISHU_SECRET','secret-1')
    service=LocalFeishuService(config,catalog,tmp_path/'state')
    api=Api()
    async def source(reference,binding):
        return FeishuSource(api,bitable_tables={x['table_id']:x['view_id'] for x in binding['bitable']['tables']})
    service._bound_source=source
    asyncio.run(service.sync('source_1',idempotency_key='initial'))
    revision=asyncio.run(service.bitable.describe('source_1','tbl_1'))['schema_revision']
    yield service,api,engine,revision,config,catalog
    service.sync_service.close();engine.dispose()


def query(fixture,**changes):
    service,api,engine,revision,*_=fixture
    body={'table_id':'tbl_1','schema_revision':revision,'field_names':['Name'],'page_size':1,'cursor':'','principal_id':'alice'}
    body.update(changes)
    return asyncio.run(service.bitable.query('source_1',**body))


def change_config(engine,mutate):
    with Session(engine) as session,session.begin():
        connector=session.get(KnowledgeConnector,'source_1')
        data=copy.deepcopy(connector.config_json);mutate(data);connector.config_json=data


def test_live_page_projects_fields_keeps_no_rows_and_binds_view(fixture,tmp_path):
    service,api,*_=fixture
    before={p.relative_to(tmp_path):p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    result=query(fixture)
    assert result['records']==[{'record_id':'rec_1','fields':{'Name':'ROW_CANARY_47021'}}]
    assert result['row_storage'] is False and result['next_cursor']
    assert api.calls==[{'app_token':'app_1','table_id':'tbl_1','view_id':'view_1','field_names':['Name'],'page_size':1,'page_token':''}]
    after={p.relative_to(tmp_path):p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    assert before==after
    assert not any(b'ROW_CANARY_47021' in value for value in after.values())
    api.page={'items':[],'has_more':False,'page_token':''}
    assert query(fixture,cursor=result['next_cursor'])['next_cursor']==''
    assert api.calls[-1]['page_token']=='provider_next'


@pytest.mark.parametrize('changes',[{'principal_id':'bob'},{'field_names':['Secret']},{'page_size':2}])
def test_cursor_cannot_change_query_identity(fixture,changes):
    cursor=query(fixture)['next_cursor'];calls=len(fixture[1].calls)
    with pytest.raises(BitableError,match='Cursor'):query(fixture,cursor=cursor,**changes)
    assert len(fixture[1].calls)==calls


def test_expired_tampered_and_policy_cursor_rejected(fixture):
    service,api,engine,*_=fixture
    cursor=query(fixture)['next_cursor']
    payload=json.loads(service.vault.vault.decrypt(cursor.encode(),context='bitable-cursor'))
    payload['expires']=int(time.time())-1
    expired=service.vault.vault.encrypt(json.dumps(payload).encode(),context='bitable-cursor').decode()
    for invalid in [expired,cursor[:-10]+'broken']:
        with pytest.raises(BitableError,match='Cursor'):query(fixture,cursor=invalid)
    change_config(engine,lambda data:data.update(bitable_policy_generation=99))
    with pytest.raises(BitableError,match='Cursor'):query(fixture,cursor=cursor)
    assert len(api.calls)==1


def test_schema_drift_before_or_during_records_requires_resync(fixture):
    service,api,*_=fixture
    api.fields[0]['field_name']='Renamed'
    with pytest.raises(BitableError,match='schema changed'):query(fixture)
    assert api.calls==[]
    api.fields[0]['field_name']='Name'
    api.hook=lambda:api.fields[0].update(type=2)
    with pytest.raises(BitableError,match='schema changed'):query(fixture)
    assert len(api.calls)==1


def test_scope_change_during_query_discards_page(fixture):
    service,api,engine,*_=fixture
    api.hook=lambda:change_config(engine,lambda data:data.update(bitable={'tables':[],'relations':[]}))
    with pytest.raises(BitableError):query(fixture)
    assert len(api.calls)==1


def test_unapproved_and_no_longer_visible_tables_are_denied(fixture):
    with pytest.raises(BitableError,match='scope'):query(fixture,table_id='tbl_2')
    fixture[1].tables=[]
    with pytest.raises(ValueError,match='visible'):query(fixture)
    assert fixture[1].calls==[]


@pytest.mark.parametrize('page',[
    {'items':[], 'has_more':'true'},
    {'items':[None], 'has_more':False},
    {'items':[{'record_id':'r','fields':{}},{'record_id':'r','fields':{}}], 'has_more':False},
    {'items':[], 'has_more':True,'page_token':{}},
])
def test_malformed_provider_pages_fail_closed(fixture,page):
    fixture[1].page=page
    with pytest.raises(BitableError):query(fixture)


def test_policy_cas_restart_and_immediate_asset_revocation(fixture,tmp_path):
    service,api,engine,revision,config,catalog=fixture
    described=asyncio.run(service.bitable.describe('source_1','tbl_1'))
    uri=described['schema_resource_uri']
    assert service.read_published(uri)
    policy=service.bitable.policy('source_1')
    result=asyncio.run(service.bitable.configure('source_1',policy={'tables':[],'relations':[]},expected_revision=policy['policy_revision']))
    assert result['tables']==[]
    with pytest.raises(LookupError):service.read_published(uri)
    with pytest.raises(BitableError,match='revision'):asyncio.run(service.bitable.configure('source_1',policy={'tables':[],'relations':[]},expected_revision=policy['policy_revision']))
    restarted=LocalFeishuService(config,catalog,tmp_path/'state')
    try:assert restarted.bitable.policy('source_1')['tables']==[]
    finally:restarted.sync_service.close()


def test_forged_asset_source_identity_rejected(fixture):
    service,api,engine,*_=fixture
    with Session(engine) as session,session.begin():
        asset=session.scalar(select(KnowledgeAsset).where(KnowledgeAsset.kind=='table_schema'))
        asset.metadata_json={'source_item_id':'wrong'}
    with pytest.raises(BitableError,match='Asset binding'):query(fixture)
    assert api.calls==[]


def test_new_relations_require_existing_synced_fields(fixture):
    service,*_=fixture
    policy=service.bitable.policy('source_1')
    relation={'id':'r','source_table_id':'tbl_1','source_field_id':'fld_1','target_table_id':'tbl_1','target_field_id':'missing','cardinality':'many_to_many'}
    with pytest.raises(BitableError,match='endpoints'):
        asyncio.run(service.bitable.configure('source_1',policy={'tables':policy['tables'],'relations':[relation]},expected_revision=policy['policy_revision']))
    relation['target_field_id']='fld_2'
    result=asyncio.run(service.bitable.configure('source_1',policy={'tables':policy['tables'],'relations':[relation]},expected_revision=policy['policy_revision']))
    assert len(result['relations'])==1
    assert service.bitable.relations('source_1')['valid']


def test_routes_enforce_scope_and_oauth_owner(fixture):
    service,api,engine,revision,*_=fixture
    current={'who':Principal('bob',scopes=('knowledge.query','knowledge.space:space_kb_default'))}
    app=FastAPI();app.include_router(create_bitable_router(service.bitable,principal_provider=lambda:current['who']))
    body={'table_id':'tbl_1','schema_revision':revision,'field_names':['Name'],'page_size':1,'cursor':''}
    with TestClient(app) as client:
        result=client.post('/v1/sources/source_1/bitable/query',json=body)
        assert result.json()['status']=='ok',result.json()
        assert result.headers['cache-control']=='no-store'
        assert client.get('/v1/sources/source_1/bitable/policy').json()['error']['code']=='permission_denied'
        change_config(engine,lambda data:data.update(auth_type='user',oauth_principal='alice'))
        for method,path,kwargs in [('get','/bitable/sources',{}),('get','/sources/source_1/bitable/tables/tbl_1/schema',{}),('get','/sources/source_1/bitable/relations',{}),('post','/sources/source_1/bitable/query',{'json':body})]:
            result=getattr(client,method)('/v1'+path,**kwargs).json()
            assert result['data']['sources']==[] if path=='/bitable/sources' else result['status']=='error'
        assert len(api.calls)==1
        current['who']=Principal('alice',scopes=('knowledge.query',))
        assert client.post('/v1/sources/source_1/bitable/query',json=body).json()['error']['code']=='permission_denied'


def test_concurrent_policy_writers_cannot_both_win_same_revision(fixture):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    service,api,*_=fixture
    barrier=Barrier(2)
    async def visible(**kwargs):
        barrier.wait(timeout=5)
        return copy.deepcopy(api.tables)
    api.list_bitable_tables=visible
    revision=service.bitable.policy('source_1')['policy_revision']
    def configure(view):
        try:
            return asyncio.run(service.bitable.configure('source_1',policy={'tables':[{'table_id':'tbl_1','view_id':view}],'relations':[]},expected_revision=revision))
        except BitableError:
            return 'stale'
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(configure,['view_a','view_b']))
    assert sum(result=='stale' for result in results)==1
    winner=next(result for result in results if isinstance(result,dict))
    assert service.bitable.policy('source_1')['tables']==winner['tables']
