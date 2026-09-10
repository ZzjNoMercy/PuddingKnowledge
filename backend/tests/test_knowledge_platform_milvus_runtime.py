"""Opt-in real Docker Milvus + independent application process acceptance.

KNOWLEDGE_TEST_REAL_MILVUS=1 python -m pytest tests/test_knowledge_platform_milvus_runtime.py
Fresh project/Home/ports only; never connects to a pre-existing Milvus endpoint.
"""
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import subprocess
import time

import pytest

from test_knowledge_platform_package_runtime import Runtime
from test_knowledge_platform_local_vector_runtime import Embeddings
from knowledge_platform.retrieval.milvus_http import MilvusVectorStore


@pytest.fixture(scope='module')
def milvus(tmp_path_factory):
    if os.environ.get('KNOWLEDGE_TEST_REAL_MILVUS') != '1':
        pytest.skip('explicit isolated Docker acceptance is not enabled')
    home = tmp_path_factory.mktemp('milvus-infrastructure')
    project = 'puddingknowledge-runtime-' + secrets.token_hex(6)
    def port():
        with socket.socket() as s:
            s.bind(('127.0.0.1', 0)); return s.getsockname()[1]
    service_port, health_port = port(), port()
    while service_port == health_port: health_port = port()
    env = {k:v for k,v in os.environ.items() if not k.startswith(('PUDDINGKNOWLEDGE_', 'COMPOSE_'))}
    env.update(PUDDINGKNOWLEDGE_HOME=str(home), PUDDINGKNOWLEDGE_MINIO_ROOT_USER='proof'+secrets.token_hex(6),
        PUDDINGKNOWLEDGE_MINIO_ROOT_PASSWORD=secrets.token_hex(24), PUDDINGKNOWLEDGE_POSTGRES_USER='proof',
        PUDDINGKNOWLEDGE_POSTGRES_PASSWORD=secrets.token_hex(24), PUDDINGKNOWLEDGE_MILVUS_PORT=str(service_port),
        PUDDINGKNOWLEDGE_MILVUS_HTTP_PORT=str(health_port))
    env.update({f'PUDDINGKNOWLEDGE_{s}_IMAGE':'unused/proof:never-started' for s in ('API','WORKER','CONSOLE')})
    for part in ('postgres','milvus/etcd','milvus/minio','milvus/data'):
        (home/'infrastructure'/part).mkdir(parents=True)
    asset = Path(__file__).parents[2]/'packages/knowledge-platform-deploy-cli/assets/compose.platform.yml'
    base = ['docker','compose','--env-file',os.devnull,'--project-name',project,'--file',str(asset)]
    def compose(*args):
        result = subprocess.run(base+list(args), env=env, capture_output=True, text=True, timeout=240)
        assert result.returncode == 0, result.stderr[-2000:]
        return result.stdout
    before = set(subprocess.check_output(['docker','ps','-q'],text=True).split())
    try:
        compose('up','-d','--pull','never','--wait','--wait-timeout','180','milvus')
        yield 'http://127.0.0.1:'+str(service_port), compose
    finally:
        compose('down','--timeout','30')
        assert not compose('ps','-q').strip()
        assert before <= set(subprocess.check_output(['docker','ps','-q'],text=True).split())


def test_milvus_store_real_integrity(milvus):
    endpoint,_ = milvus
    storage=MilvusVectorStore(endpoint=endpoint, dimension=2)
    vectors=[(1.,0.),(0.,1.)]
    name=storage.prepare(vectors,identity='sha256:'+'a'*64)
    storage.verify(name,vectors)
    assert storage.search(name,[1.,0.],2)[0][0]==0
    storage._call('entities/upsert',{'collectionName':name,'data':[{'id':0,'vector':[0.,1.]}]})
    with pytest.raises(Exception,match='content changed'):
        storage.verify(name,vectors)


def test_application_milvus_rebuild_restart_loss_and_repair(tmp_path,milvus):
    endpoint,compose=milvus
    embedding=Embeddings(); runtime=Runtime(tmp_path/'runtime')
    runtime.source.write_text('An automobile has four wheels.')
    config={'version':1,'provider_id':'knowledge_milvus_vector','space_ids':['space_kb_default'],
        'embedding':{'endpoint':f'http://127.0.0.1:{embedding.server.server_port}/embeddings',
            'model':'fixture-vector','dimension':2,'api_key_env':None},'batch_size':16,'max_chars':1200,
        'milvus':{'endpoint':endpoint,'api_key_env':None}}
    config_path=runtime.root/'index.json'; config_path.write_text(json.dumps(config)); runtime.extra_args=['--index-config',str(config_path)]
    try:
        runtime.start()
        uploaded=runtime.call('/v1/assets:upload',{'asset_id':'car','space_id':'space_kb_default','title':'car',
            'filename':runtime.source.name,'mime_type':'text/markdown','binding_id':'input',
            'content_digest':'sha256:'+hashlib.sha256(runtime.source.read_bytes()).hexdigest(),'idempotency_key':'car'})
        assert uploaded['status']=='ok',uploaded
        database=runtime.root/'state/catalog.sqlite3'
        with sqlite3.connect(database) as db:
            version=db.execute("SELECT version FROM knowledge_datasets WHERE id='portable_files'").fetchone()[0]
        request={'space_id':'space_kb_default','collection_id':'portable_files','collection_version':version,
            'capability':'document_rag_query','provider_id':'knowledge_milvus_vector','idempotency_key':'milvus-once'}
        built=runtime.call('/v1/indexes:rebuild',request); assert built['status']=='ok',built
        calls=len(embedding.calls)
        replay=runtime.call('/v1/indexes:rebuild',request)
        assert replay['status']=='ok' and replay['data']['index']['idempotent'],replay
        assert len(embedding.calls)==calls
        query={'query':'vehicle','space_id':'space_kb_default','collection_id':'portable_files','limit':1}
        def check():
            found=runtime.call('/v1/knowledge/query',query)
            assert found['status']=='ok' and 'automobile' in found['evidence'][0]['quote'],found
            mcp=runtime.call('/mcp',{'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'knowledge_query','arguments':query}})
            assert not mcp.get('error') and mcp['result']['structuredContent']['status']=='ok',mcp
        check(); runtime.stop(); runtime.start(); check()
        embedding.fail=True
        failed=runtime.call('/v1/indexes:rebuild',{**request,'idempotency_key':'failed-build'})
        assert failed['status']=='error',failed
        embedding.fail=False; check()
        with sqlite3.connect(database) as db:
            name=db.execute('SELECT collection_name FROM knowledge_vector_locations WHERE generation=1').fetchone()[0]
        storage=MilvusVectorStore(endpoint=endpoint,dimension=2)
        storage._call('collections/drop',{'collectionName':name})
        assert runtime.call('/v1/knowledge/query',query)['status']=='error'
        assert runtime.call('/v1/indexes:rebuild',request)['status']=='error'
        rebuilt=runtime.call('/v1/indexes:rebuild',{**request,'idempotency_key':'repair-lost'})
        assert rebuilt['status']=='ok' and rebuilt['data']['index']['generation']==2,rebuilt
        check()
        runtime.stop()
        compose('down','--timeout','30')
        compose('up','-d','--pull','never','--wait','--wait-timeout','180','milvus')
        runtime.start()
        # Process health precedes recovery of loaded collections. The public
        # API may refuse a transient query; retry reads within a bounded window.
        deadline=time.monotonic()+90
        while True:
            try:
                check();break
            except (AssertionError,TimeoutError):
                if time.monotonic()>=deadline:raise
                time.sleep(2)
    finally:
        runtime.stop();embedding.close()
