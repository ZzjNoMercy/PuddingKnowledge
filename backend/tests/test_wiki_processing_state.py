import asyncio
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient
from knowledge_contracts import Principal
from knowledge_platform.catalog.sqlite_query import SqliteCatalogQueryRepository
from knowledge_platform.local.app import _build_app
from knowledge_platform.local.workspace import open_persistent_workspace
from knowledge_platform.local.wiki import build_wiki_services
from knowledge_platform.local.wiki_processing_state import WikiProcessingStateService
from knowledge_platform.wiki.compiler import WikiCompilationRequest
from test_migrated_wiki_lineage import make
from test_knowledge_platform_local_wiki_processing import _install_fake_model, _FakeModel


def setup(tmp_path,monkeypatch):
    _install_fake_model(monkeypatch)
    state=tmp_path/'state'
    with open_persistent_workspace(state,wiki_archive=make(tmp_path)) as owned:
        repo=SqliteCatalogQueryRepository(owned['catalog'])
        raw=next(a for a in repo.list_assets(space_id=owned['space_id']) if a['kind']=='raw_snapshot' and a['title']=='a.md')
        config={'version':2,'space_id':raw['space_id'],'assets':{raw['id']:str(owned['raw_bindings'][raw['id']])},'model':{'endpoint':'http://127.0.0.1:9999/v1/chat/completions','model':'fixture'}}
    principal=Principal(subject_id='test',scopes=('knowledge.read',f"knowledge.space:{raw['space_id']}"))
    services=build_wiki_services(config,state/'catalog.sqlite3',state/'processing/wiki')
    request=WikiCompilationRequest(raw['id'],raw['revision'],raw['source_uri'],raw['content_digest'],'once')
    return state,raw,principal,services,request


def test_history_to_current_publication_and_restart(tmp_path,monkeypatch):
    state,raw,principal,services,request=setup(tmp_path,monkeypatch)
    reader=WikiProcessingStateService(state/'catalog.sqlite3')
    before=reader.read(raw['id'],principal)
    assert before['coverage']=='historical_consumption_recorded'
    assert before['current_publications']==[]
    result=asyncio.run(services.wiki_compilation.compile(request))
    for _ in range(2):
        with open_persistent_workspace(state) as owned:
            app=_build_app(SqliteCatalogQueryRepository(owned['catalog']),owned['file_bindings'],principal)
            response=TestClient(app).get(f"/v1/wiki/assets/{raw['id']}/processing")
            assert response.json()['status']=='ok',response.text
            data=response.json()['data']['processing']
            assert data['coverage']=='current_revision_compiled'
            assert data['historical']['historical_consumed'] is True
            assert [p['resource_uri'] for p in data['current_publications']]==[result.resource_uri]
            assert data['live_execution_known'] is False and data['automatic_scheduling_allowed'] is False
            assert 'receipt_id' not in response.text and str(state) not in response.text


def test_failed_attempt_is_not_live_worker(tmp_path,monkeypatch):
    state,raw,principal,services,request=setup(tmp_path,monkeypatch)
    async def fail(*args,**kwargs):raise RuntimeError('model failure')
    monkeypatch.setattr(_FakeModel,'generate',fail)
    with pytest.raises(Exception):asyncio.run(services.wiki_compilation.compile(request))
    result=WikiProcessingStateService(state/'catalog.sqlite3').read(raw['id'],principal)
    assert result['coverage']=='historical_consumption_recorded'
    assert result['unsettled_attempts']==1 and result['live_execution_known'] is False


@pytest.mark.parametrize('scopes,tenant',[((),None),(('knowledge.read',),None),(('knowledge.admin',),None),(('knowledge.read','knowledge.space:wrong'),None),(('knowledge.read','knowledge.space:all'),'tenant')])
def test_scope_denial(tmp_path,monkeypatch,scopes,tenant):
    state,raw,_,_,_=setup(tmp_path,monkeypatch)
    principal=Principal(subject_id='test',scopes=scopes,tenant_id=tenant)
    with pytest.raises(PermissionError):WikiProcessingStateService(state/'catalog.sqlite3').read(raw['id'],principal)


@pytest.mark.parametrize('change',['status_only','markdown','fingerprint','output_digest','output_source','output_metadata','receipt'])
def test_success_requires_consistent_publication(tmp_path,monkeypatch,change):
    state,raw,principal,services,request=setup(tmp_path,monkeypatch)
    result=asyncio.run(services.wiki_compilation.compile(request))
    output=result.resource_uri.rsplit('/',1)[1]
    with sqlite3.connect(state/'catalog.sqlite3') as db:
        if change=='status_only':db.execute('DELETE FROM knowledge_assets WHERE id=?',(output,))
        elif change=='markdown':db.execute("UPDATE knowledge_local_wiki_compilations SET markdown=X'666f72676564'")
        elif change=='fingerprint':db.execute("UPDATE knowledge_local_wiki_compilations SET fingerprint='sha256:forged'")
        elif change=='output_digest':db.execute("UPDATE knowledge_assets SET content_digest='sha256:forged' WHERE id=?",(output,))
        elif change=='output_source':db.execute("UPDATE knowledge_assets SET source_type='other' WHERE id=?",(output,))
        elif change=='output_metadata':db.execute("UPDATE knowledge_assets SET metadata_json='{}' WHERE id=?",(output,))
        elif change=='receipt':db.execute("UPDATE knowledge_local_wiki_compilations SET receipt_id=''")
    with pytest.raises(ValueError):WikiProcessingStateService(state/'catalog.sqlite3').read(raw['id'],principal)


def test_no_compilation_table_and_no_history_is_not_pending(tmp_path,monkeypatch):
    _install_fake_model(monkeypatch)
    state=tmp_path/'state'
    with open_persistent_workspace(state,wiki_archive=make(tmp_path)) as owned:
        repo=SqliteCatalogQueryRepository(owned['catalog'])
        raw=next(a for a in repo.list_assets(space_id=owned['space_id']) if a['title']=='b.md')
        who=Principal(subject_id='test',scopes=('knowledge.read',f"knowledge.space:{raw['space_id']}"))
        result=WikiProcessingStateService(owned['catalog']).read(raw['id'],who)
        assert result['coverage']=='no_consumption_record'
        assert result['unsettled_attempts']==0 and result['automatic_scheduling_allowed'] is False


@pytest.mark.parametrize('field', ['source_uri','content_digest','source_revision','key_digest','status'])
def test_corrupt_current_attempt_is_not_silently_ignored(tmp_path,monkeypatch,field):
    state,raw,principal,services,request=setup(tmp_path,monkeypatch)
    asyncio.run(services.wiki_compilation.compile(request))
    with sqlite3.connect(state/'catalog.sqlite3') as db:db.execute(f'UPDATE knowledge_local_wiki_compilations SET {field}=?',('corrupt',))
    with pytest.raises(ValueError):WikiProcessingStateService(state/'catalog.sqlite3').read(raw['id'],principal)


def test_old_revision_does_not_confer_current_coverage(tmp_path,monkeypatch):
    state,raw,principal,services,request=setup(tmp_path,monkeypatch)
    asyncio.run(services.wiki_compilation.compile(request))
    with sqlite3.connect(state/'catalog.sqlite3') as db:
        db.execute("UPDATE knowledge_assets SET revision=?,content_digest=? WHERE id=?",('sha256:'+'0'*64,'sha256:'+'0'*64,raw['id']))
    result=WikiProcessingStateService(state/'catalog.sqlite3').read(raw['id'],principal)
    assert result['coverage']=='historical_consumption_recorded' and not result['current_publications']


def test_oversized_publication_refused(tmp_path,monkeypatch):
    state,raw,principal,services,request=setup(tmp_path,monkeypatch)
    asyncio.run(services.wiki_compilation.compile(request))
    with sqlite3.connect(state/'catalog.sqlite3') as db:db.execute('UPDATE knowledge_local_wiki_compilations SET markdown=zeroblob(?)',(8*1024*1024+1,))
    with pytest.raises(ValueError):WikiProcessingStateService(state/'catalog.sqlite3').read(raw['id'],principal)


def test_read_uses_one_sqlite_snapshot(tmp_path,monkeypatch):
    from knowledge_platform.local import wiki_processing_state as module
    state,raw,principal,services,request=setup(tmp_path,monkeypatch)
    result=asyncio.run(services.wiki_compilation.compile(request))
    output=result.resource_uri.rsplit('/',1)[1]
    with sqlite3.connect(state/'catalog.sqlite3') as db:db.execute('PRAGMA journal_mode=WAL')
    original=module._has_table
    changed=[]
    def concurrent_write(connection,name):
        value=original(connection,name)
        if name=='knowledge_local_wiki_compilations' and not changed:
            with sqlite3.connect(state/'catalog.sqlite3') as writer:writer.execute("UPDATE knowledge_assets SET content_digest='corrupt' WHERE id=?",(output,))
            changed.append(True)
        return value
    monkeypatch.setattr(module,'_has_table',concurrent_write)
    state_reader=WikiProcessingStateService(state/'catalog.sqlite3')
    assert state_reader.read(raw['id'],principal)['coverage']=='current_revision_compiled'
    assert changed
    with pytest.raises(ValueError):state_reader.read(raw['id'],principal)


def test_repeat_publication_keys_deduplicate_output(tmp_path,monkeypatch):
    from dataclasses import replace
    state,raw,principal,services,request=setup(tmp_path,monkeypatch)
    first=asyncio.run(services.wiki_compilation.compile(request))
    second=asyncio.run(services.wiki_compilation.compile(replace(request,idempotency_key='second')))
    assert first.resource_uri==second.resource_uri
    result=WikiProcessingStateService(state/'catalog.sqlite3').read(raw['id'],principal)
    assert len(result['current_publications'])==1


def test_http_denial_has_no_asset_or_publication_details(tmp_path,monkeypatch):
    state,raw,principal,services,request=setup(tmp_path,monkeypatch)
    asyncio.run(services.wiki_compilation.compile(request))
    denied=Principal(subject_id='other',scopes=('knowledge.read','knowledge.space:other'))
    with open_persistent_workspace(state) as owned:
        client=TestClient(_build_app(SqliteCatalogQueryRepository(owned['catalog']),owned['file_bindings'],denied))
        present=client.get(f"/v1/wiki/assets/{raw['id']}/processing").json()
        missing=client.get('/v1/wiki/assets/missing/processing').json()
        assert present['error']==missing['error'] and present['error']['code']=='permission_denied'
        assert present['data']=={} and raw['space_id'] not in json.dumps(present)
