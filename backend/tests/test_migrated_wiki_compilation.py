import asyncio,hashlib,json,sqlite3
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from knowledge_contracts import Principal
from knowledge_platform.local.workspace import open_persistent_workspace
from knowledge_platform.local.wiki import build_wiki_services,load_wiki_config
from knowledge_platform.local.wiki_query import PublishedWikiReader
from knowledge_platform.catalog.sqlite_query import SqliteCatalogQueryRepository
from knowledge_platform.local.app import _build_app
from knowledge_platform.wiki.compiler import WikiCompilationRequest
from test_combined_workspace import _make
from test_knowledge_platform_local_wiki_processing import _install_fake_model


def setup(tmp_path,monkeypatch):
    _install_fake_model(monkeypatch)
    document,wiki,_=_make(tmp_path);state=tmp_path/'state'
    with open_persistent_workspace(state,document_migration=document,wiki_archive=wiki) as owned:
        asset=next(k for k in owned['file_bindings'] if k not in owned['document_bindings'])
        source=owned['file_bindings'][asset]
        with sqlite3.connect(owned['catalog']) as db:space,revision=db.execute('SELECT space_id,revision FROM knowledge_assets WHERE id=?',(asset,)).fetchone()
    config={'version':2,'space_id':space,'assets':{asset:str(source)},'model':{'endpoint':'http://127.0.0.1:9999/v1/chat/completions','model':'fixture'}}
    path=tmp_path/'config.json';path.write_text(json.dumps(config))
    return state,load_wiki_config(path),asset,revision


def test_compile_publish_query_restart_and_replay_in_migrated_space(tmp_path,monkeypatch):
    state,config,asset,revision=setup(tmp_path,monkeypatch)
    request=WikiCompilationRequest(asset,revision,f"knowledge://spaces/{config['space_id']}/assets/{asset}",revision,'once')
    expected=None
    for _ in range(2):
        with open_persistent_workspace(state) as owned:
            services=build_wiki_services(config,owned['catalog'],state/'processing/wiki')
            result=asyncio.run(services.wiki_compilation.compile(request))
            if expected is not None:assert result==expected
            expected=result
            reader=PublishedWikiReader(SqliteCatalogQueryRepository(owned['catalog']),services)
            assert services.read_published(result.resource_uri).startswith(b'# Compiled')
            with sqlite3.connect(owned['catalog']) as db:
                row=db.execute("SELECT id,asset_ids FROM knowledge_datasets WHERE id LIKE 'collection_compiled_wiki_%'").fetchone()
                assert json.loads(row[1])==[result.resource_uri.rsplit('/',1)[1]]
            app=_build_app(SqliteCatalogQueryRepository(owned['catalog']),owned['file_bindings'],Principal(subject_id='test',scopes=('knowledge.query','knowledge.read','knowledge.search',f"knowledge.space:{config['space_id']}")),wiki_provider=reader,wiki_blob_reader=reader,document_bindings=owned['document_bindings'])
            response=TestClient(app).post('/v1/query',json={'query':'wiki body','space_id':config['space_id'],'collection_id':row[0]})
            assert response.status_code==200,response.text
            assert response.json()['status']=='ok',response.text
            assert result.resource_uri in response.text,response.text


def test_unknown_space_rejected_before_model_construction(tmp_path,monkeypatch):
    state,config,asset,revision=setup(tmp_path,monkeypatch)
    config['space_id']='space_missing'
    with pytest.raises(ValueError,match='absent'):build_wiki_services(config,state/'catalog.sqlite3',state/'processing/wiki')


def test_collection_collision_rolls_back_publication(tmp_path,monkeypatch):
    state,config,asset,revision=setup(tmp_path,monkeypatch)
    cid='collection_compiled_wiki_'+hashlib.sha256(config['space_id'].encode()).hexdigest()[:32]
    with sqlite3.connect(state/'catalog.sqlite3') as db:
        db.execute("UPDATE knowledge_datasets SET id=?,kind='other' WHERE space_id=?",(cid,config['space_id']))
    services=build_wiki_services(config,state/'catalog.sqlite3',state/'processing/wiki')
    request=WikiCompilationRequest(asset,revision,f"knowledge://spaces/{config['space_id']}/assets/{asset}",revision,'collision')
    with pytest.raises(ValueError,match='another source'):asyncio.run(services.wiki_compilation.compile(request))
    with sqlite3.connect(state/'catalog.sqlite3') as db:assert db.execute("SELECT count(*) FROM knowledge_assets WHERE source_type='local_wiki_compilation'").fetchone()[0]==0
