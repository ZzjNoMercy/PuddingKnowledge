"""Real HTTP capture, owned content, restart replay and Wiki promotion."""
import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient
from knowledge_contracts import Principal
from knowledge_platform.catalog import SqliteCatalogQueryRepository
from knowledge_platform.local.app import _build_app
from knowledge_platform.local.read_later import ReadLaterService
from knowledge_platform.local.wiki import build_wiki_services
from knowledge_platform.local.wiki_query import PublishedWikiReader
from test_knowledge_platform_local_runtime import _build_minimal_catalog

@pytest.fixture
def source_server():
    calls=[]
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append(('GET',self.path))
            self.send(b'<html><title>Capture Orbit</title><article><h1>Orbit</h1><p>CAPTURE_ORBIT_921 is the source fact.</p></article></html>','text/html')
        def do_POST(self):
            calls.append(('MODEL',self.path))
            payload=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            assert 'CAPTURE_ORBIT_921' in payload['messages'][1]['content']
            self.send(json.dumps({'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'title':'Promoted Orbit','markdown':'# Promoted Orbit\nCAPTURE_ORBIT_921 is the source fact.'})}}]}).encode(),'application/json')
        def send(self,body,media):
            self.send_response(200);self.send_header('Content-Type',media);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        def log_message(self,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    yield f'http://127.0.0.1:{server.server_port}',calls
    server.shutdown();server.server_close();thread.join(timeout=5)


def test_capture_read_restart_retry_and_promote(tmp_path, source_server):
    origin,calls=source_server
    catalog=tmp_path/'catalog.sqlite3';_build_minimal_catalog(catalog)
    state=tmp_path/'processing'
    def app(scopes=('knowledge.processing','knowledge.read','knowledge.search','knowledge.space:space_kb_default')):
        service=ReadLaterService(catalog,state,allowed_origins=[origin])
        repo=SqliteCatalogQueryRepository(catalog)
        wiki=build_wiki_services({'version':1,'space_id':'space_kb_default','assets':{},'model':{'endpoint':origin+'/model','model':'fixture'}},catalog,state,captured_sources=service)
        reader=PublishedWikiReader(repo,wiki)
        return _build_app(repo,{},Principal('local',scopes),read_later=service,wiki_compilation=wiki.wiki_compilation,wiki_provider=reader,wiki_blob_reader=reader)
    body={'url':origin+'/article?token=private-source-token','space_id':'space_kb_default','idempotency_key':'capture-once'}
    with TestClient(app()) as client:
        result=client.post('/v1/captures',json=body).json()
        assert result['status']=='ok',result
        capture=result['data']['capture']; assert capture['status']=='succeeded'
        read=client.post('/v1/assets/'+capture['asset_id']+':read',json={'end':4096}).json()
        assert read['status']=='ok',read
        assert b'CAPTURE_ORBIT_921' in base64.b64decode(read['data']['content_base64'])
        assert client.post('/v1/captures',json={**body,'url':origin+'/another'}).json()['status']=='error'
        promotion=client.post('/v1/captures/assets/'+capture['asset_id']+':promote',json={'source_revision':capture['content_digest'],'idempotency_key':'promote-once'}).json()
        assert promotion['status']=='ok',promotion
        query=client.post('/v1/wiki/query',json={'query':'CAPTURE_ORBIT_921','space_id':'space_kb_default'}).json()
        assert query['status']=='ok' and query.get('evidence'),query
    with TestClient(app()) as restarted:
        retry=restarted.post('/v1/captures/jobs/'+capture['job_id']+':retry').json()
        assert retry['status']=='ok' and retry['data']['capture']==capture,retry
        assert restarted.get('/v1/captures').json()['data']['captures'][0]['asset_id']==capture['asset_id']
    assert len([x for x in calls if x[0]=='GET'])==1
    assert len([x for x in calls if x[0]=='MODEL'])==1
    raw=catalog.read_bytes()
    assert b'private-source-token' not in raw and b'capture-once' not in raw
    with TestClient(app(('knowledge.read',))) as denied:
        assert denied.post('/v1/captures',json=body).json()['status']=='error'


def test_real_process_capture_promotion_and_restart(tmp_path,source_server):
    import os, socket, subprocess, sys, time
    from pathlib import Path
    from urllib.request import Request,urlopen
    origin,calls=source_server
    catalog=tmp_path/'seed.sqlite3';_build_minimal_catalog(catalog)
    original=catalog.read_bytes()
    seed=tmp_path/'wiki';seed.mkdir();(seed/'start.md').write_text('# Seed\nInitial seed')
    capture_config=tmp_path/'capture.json';capture_config.write_text(json.dumps({'version':1,'allowed_origins':[origin]}))
    wiki_config=tmp_path/'wiki.json';wiki_config.write_text(json.dumps({'version':1,'space_id':'space_kb_default','assets':{},'model':{'endpoint':origin+'/model','model':'fixture'}}))
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    env=dict(os.environ)
    if os.environ.get('KNOWLEDGE_TEST_INSTALLED')=='1':env.pop('PYTHONPATH',None);env.pop('PYTHONHOME',None)
    else:env['PYTHONPATH']=str(Path(__file__).parents[1])
    def request(path,body):
        with urlopen(Request(f'http://127.0.0.1:{port}'+path,data=json.dumps(body).encode(),headers={'Content-Type':'application/json'}),timeout=15) as response:return json.load(response)
    proc=None
    try:
        for index in (1,2):
            ready=tmp_path/f'ready{index}.json'
            log=tmp_path/f'run{index}.log'
            with log.open('w') as output:
                proc=subprocess.Popen([sys.executable,'-m','knowledge_platform.local','--catalog',str(catalog),'--wiki-root',str(seed),
                    '--temp-dir',str(tmp_path/f'temp{index}'),'--ready-file',str(ready),'--port',str(port),
                    '--state-dir',str(tmp_path/'state'),'--capture-config',str(capture_config),'--wiki-config',str(wiki_config)],env=env,cwd=tmp_path,stdout=output,stderr=output)
            for _ in range(200):
                assert proc.poll() is None,log.read_text()
                if ready.exists():
                    try:
                        with urlopen(f'http://127.0.0.1:{port}/v1/spaces',timeout=.1):break
                    except OSError:pass
                time.sleep(.025)
            else:raise AssertionError('Runtime did not start')
            result=request('/v1/captures',{'url':origin+'/article','idempotency_key':'process-capture','space_id':'space_kb_default'})
            assert result['status']=='ok',result
            capture=result['data']['capture']
            read=request('/v1/assets/'+capture['asset_id']+':read',{'end':4096})
            assert read['status']=='ok',read
            assert b'CAPTURE_ORBIT_921' in base64.b64decode(read['data']['content_base64'])
            promotion=request('/v1/captures/assets/'+capture['asset_id']+':promote',{'source_revision':capture['content_digest'],'idempotency_key':'process-promote'})
            assert promotion['status']=='ok',promotion
            proc.terminate();proc.wait(timeout=10);proc=None
        assert len([x for x in calls if x[0]=='GET'])==1
        assert len([x for x in calls if x[0]=='MODEL'])==1
        assert catalog.read_bytes()==original
    finally:
        if proc and proc.poll() is None:proc.terminate();proc.wait(timeout=10)


def test_catalog_rejects_another_object_store(tmp_path):
    catalog=tmp_path/'catalog.sqlite3';_build_minimal_catalog(catalog)
    first=ReadLaterService(catalog,tmp_path/'first')
    with pytest.raises(ValueError,match='another object store'):
        ReadLaterService(catalog,tmp_path/'second')
    assert ReadLaterService(catalog,tmp_path/'first').objects.identity==first.objects.identity


def test_fenced_worker_cannot_publish_or_mark_new_owner_failed(tmp_path,monkeypatch):
    import asyncio,sqlite3
    from knowledge_platform.capture.http import PublicURLResponse
    catalog=tmp_path/'catalog.sqlite3';_build_minimal_catalog(catalog)
    service=ReadLaterService(catalog,tmp_path/'state')
    async def replace_owner(*args,**kwargs):
        with sqlite3.connect(catalog) as db:db.execute("UPDATE knowledge_ingestion_jobs SET lease_owner='replacement'")
        return PublicURLResponse('https://example.com','text/plain',b'Uncommitted text')
    monkeypatch.setattr('knowledge_platform.local.capture_fetch.fetch',replace_owner)
    with pytest.raises(ValueError,match='no longer owned'):
        asyncio.run(service.capture(url='https://example.com',idempotency_key='fenced'))
    with sqlite3.connect(catalog) as db:
        assert db.execute('SELECT lease_owner,status FROM knowledge_ingestion_jobs').fetchone()==('replacement','running')
        assert db.execute("SELECT COUNT(*) FROM knowledge_assets WHERE source_type='web_capture'").fetchone()==(0,)


def test_cancelled_capture_reaps_fetch_process_before_unlock(tmp_path,monkeypatch):
    import asyncio,sqlite3
    import knowledge_platform.local.capture_fetch as fetch_module
    catalog=tmp_path/'catalog.sqlite3';_build_minimal_catalog(catalog)
    service=ReadLaterService(catalog,tmp_path/'state')
    processes=[]
    original=asyncio.create_subprocess_exec
    async def slow_process(*args,**kwargs):
        import sys
        # A real owned subprocess stands in for a stuck transport/DNS resolver.
        process=await original(sys.executable,'-c','import time; time.sleep(30)',**kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(fetch_module.asyncio,'create_subprocess_exec',slow_process)
    async def scenario():
        task=asyncio.create_task(service.capture(url='https://example.com',idempotency_key='cancel'))
        while not processes:await asyncio.sleep(.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):await asyncio.wait_for(task,2)
        assert processes[0].returncode is not None
    asyncio.run(scenario())
    with sqlite3.connect(catalog) as db:
        assert db.execute('SELECT status FROM knowledge_ingestion_jobs').fetchone()==('failed',)
    from knowledge_platform.capture.http import PublicURLResponse
    async def ready(*args,**kwargs):return PublicURLResponse('https://example.com','text/plain',b'Retry after cancellation')
    monkeypatch.setattr('knowledge_platform.local.capture_fetch.fetch',ready)
    assert asyncio.run(service.capture(url='https://example.com',idempotency_key='cancel'))['status']=='succeeded'


def test_capture_preserves_raw_encoding_and_decodes_article(tmp_path,monkeypatch):
    import asyncio,hashlib
    from knowledge_platform.capture.http import PublicURLResponse
    catalog=tmp_path/'catalog.sqlite3';_build_minimal_catalog(catalog)
    service=ReadLaterService(catalog,tmp_path/'state')
    raw='<html><title>编码原文</title><article>这是一段需要完整保留的正文。</article></html>'.encode('utf-16')
    async def fetched(*args,**kwargs):return PublicURLResponse('https://example.com','text/html',raw)
    monkeypatch.setattr('knowledge_platform.local.capture_fetch.fetch',fetched)
    result=asyncio.run(service.capture(url='https://example.com',idempotency_key='encoding'))
    assert '需要完整保留' in service.read_published(result['source_uri']).decode('utf-8')
    assert service.objects.read('sha256:'+hashlib.sha256(raw).hexdigest())==raw
