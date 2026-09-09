"""Real Catalog + HTTP structured Authoring/Processing/query composition."""
import hashlib
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from knowledge_contracts import Principal
from knowledge_platform.catalog import SqliteCatalogQueryRepository, migrate_to_latest
from knowledge_platform.local.app import _build_app
from knowledge_platform.local.structured import STRUCTURED_SCOPES, build_structured_services, load_structured_config


def setup_catalog(tmp_path):
    file = tmp_path / 'source.csv'
    file.write_text('name,total\nA,3\nB,7\n')
    catalog = tmp_path / 'catalog.sqlite3'
    engine = create_engine(f'sqlite:///{catalog}')
    with engine.begin() as conn:
        migrate_to_latest(conn)
    engine.dispose()
    with sqlite3.connect(catalog) as conn:
        conn.execute("INSERT INTO knowledge_spaces VALUES ('space_kb_default','Local','','{}','now','now')")
        conn.execute("INSERT INTO knowledge_datasets VALUES ('dataset_kb_default','space_kb_default','Local','v1','document','','[]','[]','[]','{}','{}','','now','now')")
        conn.execute('''INSERT INTO knowledge_structured_assets (
            id,space_id,source_key,source_type,file_name,size_bytes,source_uri,
            source_reference_digest,logical_path_digest,profile_uri,profile_reference_digest,
            content_digest,profile_status,row_count,column_count,columns_json,reference_status,
            capabilities,metadata_json,created_at,updated_at)
            VALUES ('source1','space_kb_default','source1','local','source.csv',?,
            'knowledge://spaces/space_kb_default/structured-assets/source1/source',
            '','','','',?,'ready',2,2,'["name","total"]','ready','["table_query"]','{}','now','now')''',
            (file.stat().st_size, 'sha256:' + hashlib.sha256(file.read_bytes()).hexdigest()))
    return catalog, file


def client_for(catalog, file):
    return TestClient(_build_app(SqliteCatalogQueryRepository(catalog), {},
        Principal('local-test', (*STRUCTURED_SCOPES,'knowledge.list','knowledge.read',
                               'knowledge.space:space_kb_default')),
        **build_structured_services({'source1': file}, catalog)))


def test_author_publish_query_replay_and_restart(tmp_path):
    catalog, file = setup_catalog(tmp_path)
    original_source = file.read_bytes()
    with client_for(catalog, file) as client:
        response = client.post('/v1/datasets',json={'dataset_id':'combined','space_id':'space_kb_default',
            'title':'Combined','source_asset_ids':['source1'],'canonical_columns':['name','total']})
        assert response.status_code == 200, response.text
        assert response.json()['status'] == 'ok', response.text
        pending = client.post('/v1/table/query',json={'query':'A','dataset_id':'combined','space_id':'space_kb_default'})
        assert pending.json()['status'] == 'error'
        publication = client.post('/v1/datasets/combined:publish',json={
            'space_id':'space_kb_default','idempotency_key':'publish-once'}).json()
        assert publication['status'] == 'ok', publication
        query = client.post('/v1/table/query',json={'query':'A','dataset_id':'combined','space_id':'space_kb_default'}).json()
        assert query['status'] == 'ok', query
        assert query['data']['tables'][0]['preview_rows'] == [{'name':'A','total':'3'}, {'name':'B','total':'7'}]
        forbidden = client.post('/v1/datasets/combined:publish',json={
            'space_id':'space_kb_default','source_paths':{'source1':'/etc/passwd'}}).json()
        assert forbidden['status'] == 'error'
    with client_for(catalog, file) as restarted:
        replay = restarted.post('/v1/datasets/combined:publish',json={
            'space_id':'space_kb_default','idempotency_key':'publish-once'}).json()
        assert replay['status'] == 'ok', replay
        assert replay['data'] == publication['data']
    assert file.read_bytes() == original_source


def test_explicit_bindings_reject_changed_source_or_wrong_space(tmp_path):
    catalog, file = setup_catalog(tmp_path)
    before = catalog.read_bytes()
    file.write_text('different,data\n1,2\n')
    with pytest.raises(ValueError,match='differ'):
        build_structured_services({'source1':file},catalog)
    assert catalog.read_bytes() == before
    config = tmp_path / 'bindings.json'
    config.write_text(json.dumps({'version':1,'space_id':'other','assets':{'source1':str(file)}}))
    with pytest.raises(ValueError):
        load_structured_config(config)


def test_partial_admin_has_no_fake_authoring_service(tmp_path):
    catalog, _ = setup_catalog(tmp_path)
    app = _build_app(SqliteCatalogQueryRepository(catalog),{},Principal('local-test',STRUCTURED_SCOPES),
                     semantic_markdown=object())
    with TestClient(app) as client:
        result = client.post('/v1/datasets',json={}).json()
        assert result['status'] == 'error'
        assert result['error']['code'] == 'capability_unavailable'


def test_standalone_cli_structured_lifecycle(tmp_path):
    import os
    import socket
    import subprocess
    import sys
    import time
    from pathlib import Path
    from urllib.request import Request, urlopen

    catalog, file = setup_catalog(tmp_path)
    before = catalog.read_bytes()
    config = tmp_path / 'structured.json'
    config.write_text(json.dumps({'version':1,'space_id':'space_kb_default','assets':{'source1':str(file)}}))
    wiki = tmp_path / 'wiki'
    wiki.mkdir()
    (wiki / 'page.md').write_text('# Local structured integration\n')
    ready = tmp_path / 'ready.json'
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0))
        port = sock.getsockname()[1]
    env = {'PATH':os.environ['PATH']}
    python = os.environ.get('KNOWLEDGE_TEST_PYTHON',sys.executable)
    if 'KNOWLEDGE_TEST_PYTHON' not in os.environ:
        env['PYTHONPATH'] = str(Path(__file__).resolve().parents[1])
    input_catalog = catalog
    publication = None
    for run_number in range(2):
        owned_dir = tmp_path / f'run-{run_number}'
        args = [python,'-m','knowledge_platform.local','--catalog',str(input_catalog),'--wiki-root',str(wiki),
                '--structured-config',str(config),'--temp-dir',str(owned_dir),
                '--ready-file',str(ready),'--port',str(port)]
        def post(path, body):
            with urlopen(Request(f'http://127.0.0.1:{port}'+path,data=json.dumps(body).encode(),
                                 headers={'Content-Type':'application/json'}),timeout=5) as result:
                return json.load(result)
        with subprocess.Popen(args,cwd=tmp_path,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE) as child:
            try:
                for _ in range(100):
                    if child.poll() is not None:
                        pytest.fail(child.communicate()[1].decode())
                    try:
                        with urlopen(f'http://127.0.0.1:{port}/v1/spaces',timeout=.2):
                            break
                    except OSError:
                        time.sleep(.1)
                else:
                    pytest.fail('Standalone structured startup timed out')
                assert json.loads(ready.read_text())['structured_configured'] is True
                if run_number == 0:
                    assert post('/v1/datasets',{'dataset_id':'new','space_id':'space_kb_default','title':'New',
                        'source_asset_ids':['source1'],'canonical_columns':['name','total']})['status'] == 'ok'
                published = post('/v1/datasets/new:publish',{'space_id':'space_kb_default','idempotency_key':'once'})
                assert published['status'] == 'ok', published
                if publication is not None:
                    assert published['data'] == publication['data']
                publication = published
                query = post('/v1/table/query',{'query':'A','dataset_id':'new','space_id':'space_kb_default'})
                assert query['status'] == 'ok',query
                assert query['data']['tables'][0]['row_count'] == 2
            finally:
                child.terminate()
                child.communicate(timeout=10)
        assert not ready.exists()
        # Restart from the prior owned Catalog; the original source remains immutable.
        input_catalog = owned_dir / 'knowledge-platform.sqlite3'
    assert catalog.read_bytes() == before


def test_http_authoring_cannot_consume_unconfigured_catalog_sources(tmp_path):
    catalog, file = setup_catalog(tmp_path)
    with sqlite3.connect(catalog) as connection:
        row = list(connection.execute("SELECT * FROM knowledge_structured_assets WHERE id='source1'").fetchone())
        row[0], row[2] = 'unconfigured', 'unconfigured'
        connection.execute('INSERT INTO knowledge_structured_assets VALUES (' + ','.join('?' for _ in row) + ')', row)
    with client_for(catalog, file) as client:
        for space, source in [('space_kb_default','unconfigured'),('other','source1')]:
            result = client.post('/v1/datasets',json={'dataset_id':'forbidden','space_id':space,'title':'Forbidden',
                'source_asset_ids':[source],'canonical_columns':['name','total']}).json()
            assert result['status'] == 'error', result
    assert SqliteCatalogQueryRepository(catalog).get_structured_asset(asset_id='forbidden') is None


def test_wiki_snapshot_restart_updates_owned_pages_and_refuses_foreign_ids(tmp_path):
    from knowledge_platform.local.catalog import _materialize_catalog

    catalog, _ = setup_catalog(tmp_path)
    wiki = tmp_path / 'wiki'
    wiki.mkdir()
    page = wiki / 'page.md'
    page.write_text('# First publication\n')
    first = tmp_path / 'first.sqlite3'
    result = _materialize_catalog(catalog, first, wiki)
    asset_id = result['asset_ids'][0]
    page.write_text('# Updated publication\n')
    second = tmp_path / 'second.sqlite3'
    _materialize_catalog(first, second, wiki)
    with sqlite3.connect(second) as connection:
        ids = json.loads(connection.execute(
            "SELECT asset_ids FROM knowledge_datasets WHERE id='dataset_kb_default'").fetchone()[0])
        assert ids.count(asset_id) == 1
        digest = connection.execute('SELECT content_digest FROM knowledge_assets WHERE id=?',(asset_id,)).fetchone()[0]
        assert digest == 'sha256:' + hashlib.sha256(page.read_bytes()).hexdigest()
        connection.execute("UPDATE knowledge_assets SET source_type='foreign' WHERE id=?",(asset_id,))
    before = second.read_bytes()
    with pytest.raises(ValueError, match='owned by a different source'):
        _materialize_catalog(second, tmp_path / 'rejected.sqlite3', wiki)
    assert second.read_bytes() == before
