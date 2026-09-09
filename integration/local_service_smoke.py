"""Exercise stage -> deploy -> install -> start/health/stop using owned fixtures.

This is local lifecycle evidence, not production or stateful authoring acceptance.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
from urllib.request import Request, urlopen
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import base64

FIXTURE = """
import sqlite3, sys
from pathlib import Path
from sqlalchemy import create_engine
from knowledge_platform.catalog import migrate_to_latest
root=Path(sys.argv[1]); engine=create_engine(f"sqlite:///{root / 'catalog.sqlite3'}")
with engine.begin() as conn: migrate_to_latest(conn)
engine.dispose()
with sqlite3.connect(root / 'catalog.sqlite3') as conn:
 conn.execute("INSERT INTO knowledge_spaces VALUES ('space_kb_default','Local','','{}','2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')")
 conn.execute("INSERT INTO knowledge_datasets VALUES ('dataset_kb_default','space_kb_default','Local','v1','document','','[]','[]','[]','{}','{}','','2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')")
(root/'wiki').mkdir(); (root/'wiki/page.md').write_text('# Lifecycle fixture\\n\\nOWNED_SERVICE_EVIDENCE\\n')
"""


@contextmanager
def capture_source(enabled):
    if not enabled:
        yield None, []
        return
    calls=[]
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append(self.path)
            body=b'<html><title>Owned Capture</title><article>DEPLOY_CAPTURE_317</article></html>'
            self.send_response(200); self.send_header('Content-Type','text/html'); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)
        def log_message(self,*args): pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try: yield f'http://127.0.0.1:{server.server_port}',calls
    finally: server.shutdown();server.server_close();thread.join(timeout=5)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--capture', action='store_true')
    parser.add_argument('--persistent', action='store_true')
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--fixture-python', type=Path, required=True)
    args = parser.parse_args()
    repo = args.repo.resolve()
    if args.capture:
        args.persistent = True
    with tempfile.TemporaryDirectory(prefix='knowledge-service-') as directory, capture_source(args.capture) as (source_origin, source_calls):
        root = Path(directory).resolve()
        home = root / 'home'
        bundle = root / 'bundle'
        env = {**os.environ, 'PUDDINGKNOWLEDGE_HOME': str(home), 'PUDDINGCLAW_HOME': str(root / 'legacy'), 'PYTHONDONTWRITEBYTECODE': '1'}
        env.pop('PYTHONPATH', None)
        env.pop('PYTHONHOME', None)
        subprocess.run([str(args.fixture_python), '-c', FIXTURE, str(root)], env=env, cwd=root, check=True)
        before = hashlib.sha256((root / 'catalog.sqlite3').read_bytes()).hexdigest()
        subprocess.run([str(args.fixture_python), str(repo / 'packages/knowledge-platform-runtime/stage.py'), '--output', str(bundle)], env=env, cwd=root, check=True)
        cli = repo / 'packages/knowledge-platform-deploy-cli/src/cli.mjs'

        def call(*values: str, fail: bool = False) -> dict:
            result = subprocess.run(['node', str(cli), *values, '--home', str(home), '--json'], env=env, cwd=root, capture_output=True, text=True, timeout=360)
            if fail:
                assert result.returncode != 0, result.stdout
            else:
                assert result.returncode == 0, result.stdout + result.stderr
            return json.loads(result.stdout)

        call('init')
        deployed = call('deploy', '--runtime-bundle', str(bundle), '--apply')
        assert deployed['processes_started'] is False
        shutil.rmtree(bundle)
        installed = call('install', '--apply')
        assert installed['status'] == 'installed_not_running'
        assert call('install', '--apply')['reused'] is True
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        start = ('start', '--catalog', str(root / 'catalog.sqlite3'), '--wiki-root', str(root / 'wiki'), '--port', str(port), '--apply')
        state = home / 'catalog' / 'state'
        if args.persistent:
            start += ('--state-dir', str(state))
        if args.capture:
            capture_config=root / 'capture.json'
            capture_config.write_text(json.dumps({'version':1,'allowed_origins':[source_origin]}))
            start += ('--capture-config',str(capture_config))
        def post_capture():
            body={'url':source_origin+'/article','space_id':'space_kb_default','idempotency_key':'deploy-capture'}
            with urlopen(Request(f'http://127.0.0.1:{port}/v1/captures',data=json.dumps(body).encode(),headers={'Content-Type':'application/json'}),timeout=10) as response:
                result=json.load(response)
            assert result['status']=='ok',result
            return result['data']['capture']
        try:
            assert call(*start)['status'] == 'running'
            call(*start, fail=True)
            observed = call('status')
            assert observed['status'] == 'running', observed
            assert call('health')['status'] == 'running'
            with urlopen(f'http://127.0.0.1:{port}/v1/spaces', timeout=3) as response:
                assert json.load(response)['status'] == 'ok'
            if args.capture:
                captured=post_capture()
            assert call('stop', '--apply')['status'] == 'stopped'
            assert call('status')['status'] != 'running'
            if args.persistent:
                import sqlite3
                with sqlite3.connect(state / 'catalog.sqlite3') as db:
                    db.execute("UPDATE knowledge_spaces SET name='Persistent restart marker' WHERE id='space_kb_default'")
            assert call(*start)['status'] == 'running'
            if args.persistent:
                with urlopen(f'http://127.0.0.1:{port}/v1/spaces', timeout=3) as response:
                    assert 'Persistent restart marker' in response.read().decode()
            if args.capture:
                assert post_capture()==captured
                assert len(source_calls)==1
                with urlopen(Request(f"http://127.0.0.1:{port}/v1/assets/{captured['asset_id']}:read",data=b'{"end":4096}',headers={'Content-Type':'application/json'}),timeout=3) as response:
                    result=json.load(response)
                assert result['status']=='ok',result
                assert b'DEPLOY_CAPTURE_317' in base64.b64decode(result['data']['content_base64'])

        finally:
            # Stop even when startup returned malformed evidence after spawning.
            call('stop', '--apply')
        assert hashlib.sha256((root / 'catalog.sqlite3').read_bytes()).hexdigest() == before
        assert not (root / 'legacy').exists()
        release_manifest = json.loads((Path(deployed['runtime_bundle_path']) / 'manifest.json').read_text())
        supervisor_sha = release_manifest['files']['knowledge_platform/local/supervisor.py']
        assert supervisor_sha == hashlib.sha256((repo / 'backend/knowledge_platform/local/supervisor.py').read_bytes()).hexdigest()
        print(json.dumps({'status': 'passed', 'runtime_manifest_digest': deployed['runtime_manifest_digest'],
                          'supervisor_sha256': supervisor_sha,
                          'owned_bundle_survives_source_removal': True, 'locked_installed_runtime': True,
                          'real_start_health_stop_restart': True, 'duplicate_start_rejected': True,
                          'persistent_catalog_restart': args.persistent, 'capture_ingest_read_restart': args.capture, 'source_catalog_unchanged': True, 'production_activation_allowed': False}))


if __name__ == '__main__':
    main()
