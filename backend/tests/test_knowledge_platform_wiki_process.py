"""Real HTTP model and runtime processes, persistent publication and replay."""
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
from urllib.request import Request, urlopen

from test_knowledge_platform_local_runtime import _build_minimal_catalog


def test_real_wiki_compile_read_restart_replay(tmp_path):
    source = tmp_path / 'source.md'
    source.write_text('The verified process marker is ORBIT_WIKI_817.')
    digest = 'sha256:' + hashlib.sha256(source.read_bytes()).hexdigest()
    catalog = tmp_path / 'source.sqlite3'
    _build_minimal_catalog(catalog)
    with sqlite3.connect(catalog) as db:
        db.execute('''INSERT INTO knowledge_assets
          (id,space_id,kind,title,description,mime_type,source_type,source_uri,revision,content_digest,permissions_json,metadata_json,created_at,updated_at)
          VALUES ('source_a','space_kb_default','document','Source','','text/markdown','local',
          'knowledge://spaces/space_kb_default/assets/source_a',?,?, '{}','{}','now','now')''', (digest,digest))
    original = catalog.read_bytes()
    seed = tmp_path / 'seed'; seed.mkdir(); (seed / 'start.md').write_text('# Seed\nInitial page')
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            calls.append(body)
            draft = {'title': 'Orbit', 'markdown': '# Orbit\n\nORBIT_WIKI_817 is verified by the supplied source.'}
            result = {'choices':[{'finish_reason':'stop','message':{'content':json.dumps(draft)}}]}
            raw = json.dumps(result).encode()
            self.send_response(200); self.send_header('Content-Length',str(len(raw))); self.end_headers(); self.wfile.write(raw)
        def log_message(self, *args): pass
    provider = ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread = threading.Thread(target=provider.serve_forever,daemon=True); thread.start()
    config = tmp_path/'wiki.json'
    config.write_text(json.dumps({'version':1,'space_id':'space_kb_default','assets':{'source_a':str(source)},
        'model':{'endpoint':f'http://127.0.0.1:{provider.server_port}/chat','model':'controlled-fixture'}}))
    state = tmp_path/'state'
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
    def request(path, body):
        with urlopen(Request(f'http://127.0.0.1:{port}'+path,data=json.dumps(body).encode(),headers={'Content-Type':'application/json'}),timeout=10) as response:
            return json.load(response)
    def start(index):
        ready = tmp_path/f'ready{index}.json'
        log = (tmp_path/f'process{index}.log').open('w')
        # Explicit PYTHONPATH verifies current source process during development;
        # standalone installed smoke removes it before invoking this entrypoint.
        env = dict(os.environ)
        if os.environ.get('KNOWLEDGE_TEST_INSTALLED') == '1':
            env.pop('PYTHONPATH', None)
            env.pop('PYTHONHOME', None)
        else:
            env['PYTHONPATH'] = str(Path(__file__).parents[1])
        proc = subprocess.Popen([sys.executable,'-m','knowledge_platform.local','--catalog',str(catalog),'--wiki-root',str(seed),
            '--temp-dir',str(tmp_path/f'run{index}'),'--ready-file',str(ready),'--port',str(port),
            '--state-dir',str(state),'--wiki-config',str(config)],stdout=log,stderr=log,env=env,cwd=tmp_path)
        log.close()
        for _ in range(200):
            if proc.poll() is not None:
                raise AssertionError((tmp_path/f'process{index}.log').read_text())
            if ready.exists():
                try:
                    with urlopen(f'http://127.0.0.1:{port}/v1/spaces',timeout=.1): return proc
                except OSError: pass
            time.sleep(.025)
        proc.terminate(); proc.wait(timeout=10)
        raise AssertionError('runtime failed to start')
    body = {'snapshot_id':'source_a','source_revision':digest,'source_uri':'knowledge://spaces/space_kb_default/assets/source_a','content_digest':digest,'idempotency_key':'compile-once'}
    proc = None
    try:
        for index in (1,2):
            proc = start(index)
            compiled = request('/v1/wiki/assets/source_a:compile',body)
            assert compiled['status']=='ok', compiled
            uri = compiled['data']['compilation']['resource_uri']
            page_id = uri.rsplit('/',1)[-1]
            read = request(f'/v1/assets/{page_id}:read',{'end':4096})
            assert read['status']=='ok', read
            assert b'ORBIT_WIKI_817' in base64.b64decode(read['data']['content_base64'])
            query = request('/v1/wiki/query',{'query':'ORBIT_WIKI_817','space_id':'space_kb_default'})
            assert query['status']=='ok' and query.get('evidence'), query
            bad = request('/v1/wiki/assets/source_a:compile',dict(body,content_digest='sha256:'+'0'*64))
            assert bad['status']=='error', bad
            missing = request('/v1/wiki/assets/unknown:compile',dict(body,snapshot_id='unknown',source_uri='knowledge://spaces/space_kb_default/assets/unknown',idempotency_key='unknown-source'))
            assert missing['status']=='error', missing
            proc.terminate(); proc.wait(timeout=10); proc=None
            if index == 1:
                assert source.read_text()=='The verified process marker is ORBIT_WIKI_817.'
                source.unlink()  # Published content and terminal replay must survive source removal.
        assert len(calls)==1
        assert catalog.read_bytes()==original
        assert not source.exists()
    finally:
        if proc and proc.poll() is None:
            proc.terminate(); proc.wait(timeout=10)
        provider.shutdown(); provider.server_close(); thread.join(timeout=5)
