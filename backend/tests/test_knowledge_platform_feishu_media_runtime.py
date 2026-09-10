"""Real media, MinerU, derivative reads and restart in the installed runtime."""
import base64
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen

import pytest
from test_knowledge_platform_local_runtime import _build_minimal_catalog


@pytest.fixture
def remote():
    calls=[]
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            wire=self.rfile.read(int(self.headers['Content-Length']))
            if self.path=='/file_parse':
                assert b'name="files"' in wire and b'name="response_format_zip"' in wire
                calls.append('parse')
                self.send({'markdown':'# PDF-DERIVATIVE-552\n![figure](images/pic.png "caption")','assets':[{'relative_path':'images/pic.png','mime_type':'image/png','content_base64':base64.b64encode(b'PNG-DERIVATIVE-729').decode()}]})
                return
            body=json.loads(wire)
            assert self.path=='/open-apis/auth/v3/tenant_access_token/internal'
            assert body=={'app_id':'cli_fixture','app_secret':'APP_SECRET_349'}
            assert self.headers.get('Authorization') is None
            calls.append('auth')
            self.send({'code':0,'tenant_access_token':'token_fixture','expire':7200})
        def do_GET(self):
            assert self.headers.get('Authorization')=='Bearer token_fixture'
            calls.append(self.path)
            if '/medias/' in self.path:
                assert self.path=='/open-apis/drive/v1/medias/pdf_token/download'
                body=b'%PDF-1.7 ORIGINAL-PDF-883'
                self.send_response(200);self.send_header('Content-Type','application/pdf');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
                return
            if '/nodes' in self.path:
                self.send({'code':0,'data':{'items':[{'node_token':'node1','obj_token':'doc1','obj_type':'docx','title':'Remote document','has_child':False}],'has_more':False}})
            elif '/blocks' in self.path:
                assert 'document_revision_id=11' in self.path
                self.send({'code':0,'data':{'items':[{'block_id':'block1','block_type':2,'text':{'elements':[{'text_run':{'content':'FEISHU-RUNTIME-487'}}]}},{'block_id':'pdf_block','block_type':23,'file':{'token':'pdf_token','name':'same.pdf'}}],'has_more':False}})
            else:
                assert self.path=='/open-apis/docx/v1/documents/doc1'
                self.send({'code':0,'data':{'document':{'revision_id':11,'title':'Remote document'}}})
        def send(self,value):
            body=json.dumps(value).encode()
            self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        def log_message(self,*args): pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
    try: yield f'http://127.0.0.1:{server.server_port}',calls
    finally: server.shutdown();server.server_close();worker.join(timeout=5)


def test_real_process_media_parse_read_restart(tmp_path,remote):
    endpoint,calls=remote
    catalog=tmp_path/'seed.db';_build_minimal_catalog(catalog);original=catalog.read_bytes()
    wiki=tmp_path/'wiki';wiki.mkdir();(wiki/'seed.md').write_text('# Seed\nTest')
    config=tmp_path/'feishu.json'
    config.write_text(json.dumps({'version':1,'sources':[{'id':'feishu_fixture','name':'Fixture','selection':{'kind':'wiki','root':'','wiki_space':'remote_space'},'app_id':'cli_fixture','app_secret_env':'FEISHU_FIXTURE_SECRET','endpoint':endpoint,'parser':{'id':'mineru_local','endpoint':endpoint,'timeout':10}}]}))
    with socket.socket() as sock: sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    env=dict(os.environ,FEISHU_FIXTURE_SECRET='APP_SECRET_349')
    if os.environ.get('KNOWLEDGE_TEST_INSTALLED')=='1':env.pop('PYTHONPATH',None);env.pop('PYTHONHOME',None)
    else:env['PYTHONPATH']=str(Path(__file__).parents[1])
    def request(path,body=None):
        with urlopen(Request(f'http://127.0.0.1:{port}'+path,data=json.dumps(body).encode() if body is not None else None,headers={'Content-Type':'application/json'}),timeout=20) as response:
            return json.load(response)
    proc=None
    state=tmp_path/'state'
    try:
        for turn in range(2):
            ready=tmp_path/f'ready{turn}.json'
            proc=subprocess.Popen([sys.executable,'-m','knowledge_platform.local','--catalog',str(catalog),'--wiki-root',str(wiki),
                '--state-dir',str(state),'--temp-dir',str(tmp_path/f'temp{turn}'),'--ready-file',str(ready),'--port',str(port),'--feishu-config',str(config)],
                env=env,cwd='/private/tmp',stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
            for _ in range(150):
                if proc.poll() is not None:
                    raise AssertionError(proc.stderr.read().decode())
                if ready.exists():
                    try:
                        result=request('/v1/sources/feishu_fixture:sync',{'idempotency_key':'sync-once','mode':'full'})
                        break
                    except OSError: pass
                time.sleep(.05)
            else: raise AssertionError('Feishu runtime did not become ready')
            listed=request('/mcp',{'jsonrpc':'2.0','id':71,'method':'tools/list','params':{}})
            assert not any(tool['name'].startswith('feishu_bitable_') for tool in listed['result']['tools'])
            assert result['status']=='ok',result
            assert result['data']['sync']['changed']==1
            import sqlite3
            with sqlite3.connect(state/'catalog.sqlite3') as connection:
                row=connection.execute("SELECT asset_id FROM knowledge_source_items WHERE connector_id='feishu_fixture'").fetchone()
                asset_id=row[0]
            read=request('/v1/assets/'+asset_id+':read',{'end':4096})
            assert read['status']=='ok',read
            assert b'FEISHU-RUNTIME-487' in base64.b64decode(read['data']['content_base64'])
            with sqlite3.connect(state/'catalog.sqlite3') as connection:
                attachment=connection.execute("SELECT id FROM knowledge_assets WHERE kind='attachment'").fetchone()[0]
            raw=request('/v1/assets/'+attachment+':read',{'end':4096})
            assert base64.b64decode(raw['data']['content_base64'])==b'%PDF-1.7 ORIGINAL-PDF-883'
            derivatives=request('/v1/assets/'+attachment+'/derivatives')
            assert derivatives['data']['count']==1,derivatives
            normalized=request('/v1/assets/'+attachment+'/derivatives/normalized_markdown')
            assert normalized['status']=='ok',normalized
            normalized_bytes=base64.b64decode(normalized['data']['content_base64'])
            assert normalized_bytes.startswith(b'# PDF-DERIVATIVE-552')
            assert b'knowledge://spaces/' in normalized_bytes and b'images/pic.png' not in normalized_bytes
            with sqlite3.connect(state/'catalog.sqlite3') as connection:
                image=connection.execute("SELECT id FROM knowledge_assets WHERE kind='derived_media'").fetchone()[0]
            image_result=request('/v1/assets/'+image+':read',{'end':4096})
            assert base64.b64decode(image_result['data']['content_base64'])==b'PNG-DERIVATIVE-729'
            assert normalized['data']['content_digest']!=raw['data']['content_digest']
            derivative_uri=derivatives['data']['derivatives'][0]['resource_uri']
            mcp=request('/mcp',{'jsonrpc':'2.0','id':88,'method':'resources/read','params':{'uri':derivative_uri}})
            assert base64.b64decode(mcp['result']['contents'][0]['blob'])==normalized_bytes
            wrong=request('/mcp',{'jsonrpc':'2.0','id':89,'method':'resources/read','params':{'uri':derivative_uri.replace('space_kb_default','wrong_space')}})
            assert wrong['result']['structuredContent']['status']=='error'
            assert wrong['result']['contents']==[]
            assert b'./assets/' not in base64.b64decode(read['data']['content_base64'])
            if turn==1:
                incremental=request('/v1/sources/feishu_fixture:sync',{'idempotency_key':'sync-incremental','mode':'incremental'})
                assert incremental['data']['sync']['unchanged']==1,incremental
            proc.terminate();proc.wait(timeout=10);proc=None
            env.pop('FEISHU_FIXTURE_SECRET',None) # restart must resolve the owned Vault
        assert sum('/blocks' in x for x in calls)==1
        assert calls.count('parse')==1
        assert catalog.read_bytes()==original
        assert b'APP_SECRET_349' not in (state/'catalog.sqlite3').read_bytes()
    finally:
        if proc is not None:
            proc.terminate()
            try:proc.wait(timeout=5)
            except subprocess.TimeoutExpired:proc.kill();proc.wait(timeout=5)
