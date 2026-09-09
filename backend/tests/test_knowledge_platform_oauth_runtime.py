"""User OAuth, token rotation and local revocation through owned real processes."""
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

import pytest
from test_knowledge_platform_local_runtime import _build_minimal_catalog

SCOPES=['offline_access','wiki:wiki:readonly','docx:document:readonly']

@pytest.fixture
def provider():
    state={'calls':[],'challenge':None,'exchanges':0,'refreshes':0,'hold_exchange':False,
        'started':threading.Event(),'release':threading.Event(),'hold_refresh':False,'refresh_started':threading.Event()}
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            state['calls'].append(('POST',self.path))
            assert self.path=='/open-apis/authen/v2/oauth/token' # no tenant fallback
            assert body['client_id']=='cli_user' and body['client_secret']=='OAUTH_APP_SECRET'
            if body['grant_type']=='authorization_code':
                state['exchanges']+=1
                actual=base64.urlsafe_b64encode(hashlib.sha256(body['code_verifier'].encode()).digest()).rstrip(b'=').decode()
                assert actual==state['challenge']
                assert body['code']=='one-use-code'
                state['started'].set()
                if state['hold_exchange']:state['release'].wait(15)
                token,refresh='user_one','refresh_one'
            else:
                assert body['grant_type']=='refresh_token' and body['refresh_token']=='refresh_one'
                state['refreshes']+=1
                state['refresh_started'].set()
                if state['hold_refresh']:state['release'].wait(15)
                token,refresh='user_two','refresh_two'
            self.send({'code':0,'access_token':token,'refresh_token':refresh,'expires_in':3600,'refresh_token_expires_in':7200,'scope':' '.join(SCOPES)})
        def do_GET(self):
            state['calls'].append(('GET',self.path))
            auth=self.headers.get('Authorization')
            if self.path.endswith('/user_info'):
                assert auth=='Bearer user_one'
                self.send({'code':0,'data':{'open_id':'ou_fixture','tenant_key':'tenant_fixture'}})
            elif auth=='Bearer user_one':
                self.send({'code':99991661,'msg':'expired'},401)
            else:
                assert auth=='Bearer user_two'
                if '/nodes' in self.path:
                    self.send({'code':0,'data':{'items':[{'node_token':'node','obj_token':'doc','obj_type':'docx','title':'OAuth article','has_child':False}],'has_more':False}})
                elif '/blocks' in self.path:
                    self.send({'code':0,'data':{'items':[{'block_id':'block','block_type':2,'text':{'elements':[{'text_run':{'content':'OAUTH-PERSISTENCE-783'}}]}}],'has_more':False}})
                else:self.send({'code':0,'data':{'document':{'revision_id':1,'title':'OAuth article'}}})
        def send(self,value,status=200):
            body=json.dumps(value).encode();self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)));self.end_headers()
            try:self.wfile.write(body)
            except (BrokenPipeError,ConnectionResetError):pass
        def log_message(self,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
    try:yield f'http://127.0.0.1:{server.server_port}',state
    finally:state['release'].set();server.shutdown();server.server_close();worker.join(timeout=5)


class Runtime:
    def __init__(self, root, endpoint):
        self.root=root;self.proc=None;self.turn=0
        self.seed=root/'seed.db';_build_minimal_catalog(self.seed)
        wiki=root/'wiki';wiki.mkdir();(wiki/'seed.md').write_text('# Seed\nStart')
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));self.port=sock.getsockname()[1]
        self.redirect=f'http://127.0.0.1:{self.port}/v1/sources/oauth/callback'
        self.config=root/'user.json';self.config.write_text(json.dumps({'version':1,'sources':[{'id':'user_source','name':'User source',
            'selection':{'kind':'wiki','root':'','wiki_space':'remote'},'app_id':'cli_user','app_secret_env':'OAUTH_FIXTURE_SECRET',
            'endpoint':endpoint,'auth_type':'user','oauth_scopes':SCOPES,'oauth_redirect_uris':[self.redirect]}]}))
    @property
    def catalog(self):return self.root/'state'/'catalog.sqlite3'
    def start(self):
        env=dict(os.environ)
        if self.turn==0:env['OAUTH_FIXTURE_SECRET']='OAUTH_APP_SECRET'
        else:env.pop('OAUTH_FIXTURE_SECRET',None)
        if os.environ.get('KNOWLEDGE_TEST_INSTALLED')=='1':env.pop('PYTHONPATH',None);env.pop('PYTHONHOME',None)
        else:env['PYTHONPATH']=str(Path(__file__).parents[1])
        ready=self.root/f'ready{self.turn}.json'
        self.proc=subprocess.Popen([sys.executable,'-m','knowledge_platform.local','--catalog',str(self.seed),'--wiki-root',str(self.root/'wiki'),
            '--state-dir',str(self.root/'state'),'--temp-dir',str(self.root/f'temp{self.turn}'),'--ready-file',str(ready),
            '--port',str(self.port),'--feishu-config',str(self.config)],env=env,cwd='/private/tmp',stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        self.turn+=1
        for _ in range(200):
            if self.proc.poll() is not None:raise AssertionError(self.proc.stderr.read().decode())
            if ready.exists():
                try:
                    with socket.create_connection(('127.0.0.1',self.port),timeout=.1):return
                except OSError:pass
            time.sleep(.05)
        raise AssertionError('Runtime startup timed out')
    def request(self,path,body):
        with urlopen(Request(f'http://127.0.0.1:{self.port}'+path,data=json.dumps(body).encode(),headers={'Content-Type':'application/json'}),timeout=20) as response:return json.load(response)
    def stop(self,kill=False):
        if self.proc is not None:
            self.proc.kill() if kill else self.proc.terminate()
            try:self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:self.proc.kill();self.proc.wait(timeout=5)
            self.proc=None
    def begin(self,provider_state):
        begun=self.request('/v1/sources/user_source:authorize',{'redirect_uri':self.redirect})
        assert begun['status']=='ok',begun
        query=parse_qs(urlsplit(begun['data']['authorization']['authorization_url']).query)
        provider_state['challenge']=query['code_challenge'][0]
        return query['state'][0]


def test_user_oauth_rotation_restart_and_revocation(tmp_path,provider):
    endpoint,state=provider;runtime=Runtime(tmp_path,endpoint)
    try:
        runtime.start()
        assert runtime.request('/v1/sources/user_source:sync',{'idempotency_key':'before','mode':'full'})['status']=='error'
        value=runtime.begin(state)
        body={'state':value,'code':'one-use-code'}
        complete=runtime.request('/v1/sources/oauth/callback',body)
        assert complete['status']=='ok',complete
        assert runtime.request('/v1/sources/oauth/callback',body)['status']=='error'
        sync=runtime.request('/v1/sources/user_source:sync',{'idempotency_key':'first','mode':'full'})
        assert sync['status']=='ok',sync
        assert state['exchanges']==1 and state['refreshes']==1
        with sqlite3.connect(runtime.catalog) as db:
            asset_id=db.execute("SELECT asset_id FROM knowledge_source_items WHERE connector_id='user_source'").fetchone()[0]
            assert db.execute('SELECT token_version FROM knowledge_credential_grants').fetchone()[0]==2
        runtime.stop();runtime.start()
        repeated=runtime.request('/v1/sources/user_source:sync',{'idempotency_key':'second','mode':'incremental'})
        assert repeated['status']=='ok' and repeated['data']['sync']['unchanged']==1,repeated
        read=runtime.request('/v1/assets/'+asset_id+':read',{'end':4096})
        assert b'OAUTH-PERSISTENCE-783' in base64.b64decode(read['data']['content_base64'])
        assert state['exchanges']==1 and state['refreshes']==1
        assert runtime.request('/v1/sources/user_source:revoke-authorization',{})['data']['authorization']['status']=='revoked'
        before=len(state['calls']);runtime.stop();runtime.start()
        assert runtime.request('/v1/sources/user_source:sync',{'idempotency_key':'after','mode':'full'})['status']=='error'
        assert runtime.request('/v1/assets/'+asset_id+':read',{'end':4096})['status']=='error'
        assert len(state['calls'])==before
        raw=runtime.catalog.read_bytes()
        for secret in [value,'OAUTH_APP_SECRET','user_one','refresh_one','user_two','refresh_two']:
            assert secret.encode() not in raw
    finally:runtime.stop()


def test_killed_callback_is_not_exchanged_again(tmp_path,provider):
    endpoint,state=provider;runtime=Runtime(tmp_path,endpoint)
    worker=None
    try:
        runtime.start();value=runtime.begin(state);state['hold_exchange']=True
        def callback():
            try:runtime.request('/v1/sources/oauth/callback',{'state':value,'code':'one-use-code'})
            except Exception:pass
        worker=threading.Thread(target=callback,daemon=True);worker.start()
        assert state['started'].wait(5)
        with sqlite3.connect(runtime.catalog) as db:
            assert db.execute('SELECT status FROM knowledge_oauth_sessions').fetchone()[0]=='exchanging'
        runtime.stop(kill=True);state['release'].set();worker.join(timeout=5)
        runtime.start()
        assert runtime.request('/v1/sources/oauth/callback',{'state':value,'code':'one-use-code'})['status']=='error'
        assert state['exchanges']==1
        with sqlite3.connect(runtime.catalog) as db:assert db.execute('SELECT count(*) FROM knowledge_credential_grants').fetchone()[0]==0
    finally:state['release'].set();runtime.stop()


def test_killed_refresh_is_not_retried_with_old_token(tmp_path,provider):
    endpoint,state=provider;runtime=Runtime(tmp_path,endpoint)
    try:
        runtime.start();value=runtime.begin(state)
        assert runtime.request('/v1/sources/oauth/callback',{'state':value,'code':'one-use-code'})['status']=='ok'
        state['hold_refresh']=True
        def sync():
            try:runtime.request('/v1/sources/user_source:sync',{'idempotency_key':'crash-refresh','mode':'full'})
            except Exception:pass
        worker=threading.Thread(target=sync,daemon=True);worker.start()
        assert state['refresh_started'].wait(5)
        with sqlite3.connect(runtime.catalog) as db:
            assert db.execute('SELECT status FROM knowledge_credential_grants').fetchone()[0]=='refreshing'
        runtime.stop(kill=True);state['release'].set();worker.join(timeout=5)
        runtime.start()
        assert runtime.request('/v1/sources/user_source:sync',{'idempotency_key':'recover-refresh','mode':'full'})['status']=='error'
        assert state['refreshes']==1 and state['exchanges']==1
        with sqlite3.connect(runtime.catalog) as db:
            assert db.execute('SELECT status FROM knowledge_credential_grants').fetchone()[0]=='needs_reauth'
            assert db.execute('SELECT count(*) FROM knowledge_source_items').fetchone()[0]==0
    finally:state['release'].set();runtime.stop()
