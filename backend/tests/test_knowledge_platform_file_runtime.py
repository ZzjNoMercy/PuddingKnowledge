"""Installed standalone file ingestion, real FTS retrieval, and crash recovery."""
import base64
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.request import Request,urlopen

from test_knowledge_platform_local_runtime import _build_minimal_catalog


class Runtime:
    def __init__(self,root,*,parsers=None,filename='upload.md',content=b'File runtime SEARCHABLE-CANARY-229'):
        self.root=root;self.source=root/filename;self.source.write_bytes(content)
        self.seed=root/'seed.db';_build_minimal_catalog(self.seed)
        wiki=root/'wiki';wiki.mkdir();(wiki/'seed.md').write_text('# Seed\nOnly Wiki')
        self.config=root/'files.json'
        self.config.write_text(json.dumps({'version':1,'bindings':[{'id':'upload','path':str(self.source),'space_id':'space_kb_default'}],
            'parsers':parsers or [{'id':'native'}],'collection_id':'uploaded_files'}))
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));self.port=sock.getsockname()[1]
        self.proc=None;self.turn=0
    def start(self):
        self.turn+=1;ready=self.root/f'ready{self.turn}.json'
        env=dict(os.environ)
        if os.environ.get('KNOWLEDGE_TEST_INSTALLED')=='1':env.pop('PYTHONPATH',None);env.pop('PYTHONHOME',None)
        else:env['PYTHONPATH']=str(Path(__file__).parents[1])
        self.proc=subprocess.Popen([sys.executable,'-m','knowledge_platform.local','--catalog',str(self.seed),'--wiki-root',str(self.root/'wiki'),
            '--state-dir',str(self.root/'state'),'--temp-dir',str(self.root/f'temp{self.turn}'),'--ready-file',str(ready),'--port',str(self.port),'--file-config',str(self.config)],
            cwd='/private/tmp',env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        for _ in range(150):
            if self.proc.poll() is not None:raise AssertionError(self.proc.stderr.read().decode())
            if ready.exists():return
            time.sleep(.05)
        raise AssertionError('File runtime did not start')
    def stop(self,kill=False):
        if self.proc is not None:
            self.proc.kill() if kill else self.proc.terminate()
            try:self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:self.proc.kill();self.proc.wait(timeout=5)
            self.proc=None
    def request(self,path,body=None):
        with urlopen(Request(f'http://127.0.0.1:{self.port}'+path,data=json.dumps(body).encode() if body is not None else None,
            headers={'Content-Type':'application/json'}),timeout=20) as response:return json.load(response)
    def upload(self,key='one'):
        return self.request('/v1/assets:upload',{'asset_id':'imported','space_id':'space_kb_default','title':'Imported file','filename':self.source.name,
            'mime_type':'application/pdf' if self.source.suffix=='.pdf' else 'text/markdown','binding_id':'upload',
            'content_digest':'sha256:'+hashlib.sha256(self.source.read_bytes()).hexdigest(),'idempotency_key':key})


def test_file_import_real_process_search_replace_and_restart(tmp_path):
    runtime=Runtime(tmp_path);seed=runtime.seed.read_bytes()
    try:
        for turn in range(2):
            runtime.start()
            imported=runtime.upload();assert imported['status']=='ok',imported
            upload=imported['data']['upload'];assert upload['indexed'] is True
            query={'query':'SEARCHABLE-CANARY-229','space_id':'space_kb_default','limit':10}
            found=runtime.request('/v1/document-rag/query',query)
            assert found['status']=='ok' and found['data']['count']==1,found
            assert found['evidence'][0]['asset_id']==upload['normalized_asset_id']
            routed=runtime.request('/v1/knowledge/query',{**query,'collection_id':'uploaded_files','capability_hint':'document_rag_query'})
            assert routed['status']=='ok' and routed['evidence'],routed
            mcp=runtime.request('/mcp',{'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'document_rag_query','arguments':query}})
            assert mcp['result']['structuredContent']['evidence'],mcp
            read=runtime.request('/v1/assets/imported:read',{'end':4096})
            assert base64.b64decode(read['data']['content_base64'])==runtime.source.read_bytes()
            job=runtime.request('/v1/files/jobs/'+upload['job_id'])
            assert job['data']['job']['status']=='succeeded'
            runtime.stop()
        runtime.start();old=upload['normalized_asset_id']
        runtime.source.write_bytes(b'New version REPLACEMENT-CANARY-775')
        changed=runtime.upload('replacement');assert changed['status']=='ok',changed
        stale=runtime.request('/v1/assets/'+old+':read',{'end':4096});assert stale['status']=='error'
        found=runtime.request('/v1/document-rag/query',query);assert found['data']['count']==0,found
        assert runtime.seed.read_bytes()==seed
        with sqlite3.connect(tmp_path/'state/catalog.sqlite3') as conn:
            assert conn.execute("SELECT count(*) FROM knowledge_datasets WHERE id='uploaded_files'").fetchone()[0]==1
    finally:runtime.stop()


def test_sigkill_during_parser_has_no_partial_publication_and_retry(tmp_path):
    entered=threading.Event();release=threading.Event();calls=[]
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body=self.rfile.read(int(self.headers['Content-Length']));assert b'name="files"' in body
            calls.append(1)
            if len(calls)==1:entered.set();release.wait(timeout=15)
            payload=json.dumps({'markdown':'# PARSED-AFTER-CRASH-981','assets':[]}).encode()
            try:
                self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(payload)));self.end_headers();self.wfile.write(payload)
            except (BrokenPipeError,ConnectionResetError):pass
        def log_message(self,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    runtime=Runtime(tmp_path,filename='upload.pdf',content=b'%PDF fixture',parsers=[{'id':'mineru_local','endpoint':f'http://127.0.0.1:{server.server_port}','timeout':20}])
    errors=[]
    def upload():
        try:runtime.upload()
        except Exception as error:errors.append(type(error).__name__)
    try:
        runtime.start();request_thread=threading.Thread(target=upload);request_thread.start()
        assert entered.wait(timeout=10)
        runtime.stop(kill=True);release.set();request_thread.join(timeout=5)
        with sqlite3.connect(tmp_path/'state/catalog.sqlite3') as conn:
            assert conn.execute("SELECT count(*) FROM knowledge_assets WHERE source_type='local_file'").fetchone()[0]==0
            assert conn.execute("SELECT count(*) FROM file_chunks").fetchone()[0]==0
            assert conn.execute("SELECT status FROM knowledge_ingestion_jobs WHERE kind='file_import'").fetchone()[0]=='running'
        runtime.start();result=runtime.upload();assert result['status']=='ok',result
        assert result['data']['upload']['attempt']==2
        search=runtime.request('/v1/document-rag/query',{'query':'PARSED-AFTER-CRASH-981','space_id':'space_kb_default','limit':5})
        assert search['status']=='ok' and search['evidence'],search
        assert len(calls)==2
    finally:release.set();runtime.stop();server.shutdown();server.server_close();thread.join(timeout=5)
