"""Independent process Package export/import with persistent query evidence."""
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import time
from urllib.request import Request, urlopen

from test_knowledge_platform_local_runtime import _build_minimal_catalog
from test_knowledge_platform_local_package_import import _package


class Runtime:
    def __init__(self, root, *, imports=None):
        root.mkdir(); self.root = root; self.proc = None; self.turn = 0
        self.seed = root / 'seed.db'; _build_minimal_catalog(self.seed)
        wiki = root / 'wiki'; wiki.mkdir(); (wiki / 'seed.md').write_text('# Seed\nOriginal wiki')
        self.source = root / 'input.md'; self.source.write_text('# PORTABLE-CANARY-872\nRestored without original source.')
        (root / 'files.json').write_text(json.dumps({'version': 1, 'collection_id': 'portable_files', 'parsers': [{'id': 'native'}],
            'bindings': [{'id': 'input', 'path': str(self.source), 'space_id': 'space_kb_default'}]}))
        self.archive = root / 'export.zip'
        (root / 'packages.json').write_text(json.dumps({'version': 1, 'space_ids': ['space_kb_default'],
            'imports': imports or [], 'exports': [{'id': 'output', 'path': str(self.archive)}]}))
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0)); self.port = sock.getsockname()[1]

    def start(self):
        self.turn += 1; ready = self.root / f'ready{self.turn}.json'
        env = dict(os.environ)
        if os.environ.get('KNOWLEDGE_TEST_INSTALLED') == '1':
            env.pop('PYTHONPATH', None); env.pop('PYTHONHOME', None)
        else:
            env['PYTHONPATH'] = str(Path(__file__).parents[1])
        self.log = (self.root / f'run{self.turn}.log').open('wb')
        self.proc = subprocess.Popen([sys.executable, '-m', 'knowledge_platform.local',
            '--catalog', str(self.seed), '--wiki-root', str(self.root / 'wiki'),
            '--state-dir', str(self.root / 'state'), '--temp-dir', str(self.root / f'tmp{self.turn}'),
            '--ready-file', str(ready), '--port', str(self.port),
            '--file-config', str(self.root / 'files.json'), '--package-config', str(self.root / 'packages.json'),
            *getattr(self, 'extra_args', ())],
            cwd='/private/tmp', env=env, stdout=self.log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not ready.exists() and self.proc.poll() is None:
            time.sleep(.05)
        assert ready.exists(), (self.root / f'run{self.turn}.log').read_text()

    def stop(self):
        if self.proc is not None:
            self.proc.terminate(); self.proc.wait(timeout=10); self.proc = None; self.log.close()

    def call(self, path, body=None):
        request = Request(f'http://127.0.0.1:{self.port}{path}',
            data=json.dumps(body).encode() if body is not None else None,
            headers={'Content-Type': 'application/json'})
        with urlopen(request, timeout=20) as response:
            return json.load(response)


def test_export_import_query_restart_and_source_removal(tmp_path):
    source = Runtime(tmp_path / 'source'); target = None
    try:
        source.start()
        request = {'asset_id': 'original_portable', 'space_id': 'space_kb_default', 'title': 'Portable',
            'filename': 'input.md', 'mime_type': 'text/markdown', 'binding_id': 'input',
            'content_digest': 'sha256:' + hashlib.sha256(source.source.read_bytes()).hexdigest(), 'idempotency_key': 'upload'}
        uploaded = source.call('/v1/assets:upload', request)
        assert uploaded['status'] == 'ok', uploaded
        with sqlite3.connect(source.root / 'state' / 'catalog.sqlite3') as db:
            version = db.execute("SELECT version FROM knowledge_datasets WHERE id='portable_files'").fetchone()[0]
        exported = source.call('/v1/packages:export', {'output_ref': 'output', 'package_id': 'portable',
            'version': '1', 'collections': [{'id': 'portable_files', 'version': version}]})
        assert exported['status'] == 'ok', exported
        archive_digest = 'sha256:' + hashlib.sha256(source.archive.read_bytes()).hexdigest()
        target = Runtime(tmp_path / 'target', imports=[{'id': 'restore', 'path': str(source.archive), 'digest': archive_digest}])
        target.start()
        imported = target.call('/v1/packages:import', {'package_ref': 'restore', 'idempotency_key': 'restore-1'})
        assert imported['status'] == 'ok', imported
        assert imported['data']['package_revision'] == exported['data']['package_revision']
        derivative = target.call('/v1/assets/original_portable/derivatives/normalized_markdown')
        assert derivative['status'] == 'ok', derivative
        source.stop(); source.source.unlink()
        query = {'query': 'PORTABLE-CANARY-872', 'space_id': 'space_kb_default', 'limit': 10}
        result = target.call('/v1/document-rag/query', query)
        assert result['status'] == 'ok' and result['evidence'], result
        routed = target.call('/v1/knowledge/query', {**query, 'collection_id': 'portable_files'})
        assert routed['status'] == 'ok' and routed['evidence'], routed
        target.stop(); target.start()
        replay = target.call('/v1/packages:import', {'package_ref': 'restore', 'idempotency_key': 'restore-1'})
        assert replay['status'] == 'ok' and replay['data']['idempotent'], replay
        result = target.call('/v1/document-rag/query', query)
        assert result['status'] == 'ok' and result['evidence'], result
        refused = target.call('/v1/assets:upload', {**request, 'asset_id': 'attempted_local_overwrite', 'idempotency_key': 'new-local-upload'})
        assert refused['status'] == 'error', refused
        routed = target.call('/v1/knowledge/query', {**query, 'collection_id': 'portable_files'})
        assert routed['status'] == 'ok' and routed['evidence'], routed
        with sqlite3.connect(target.root / 'state' / 'catalog.sqlite3') as db:
            assert db.execute('SELECT COUNT(*) FROM knowledge_package_imports').fetchone()[0] == 1
    finally:
        source.stop()
        if target is not None: target.stop()


def test_sigkill_before_package_commit_has_no_partial_catalog_and_can_retry(tmp_path):
    archive = _package(tmp_path)
    catalog = tmp_path / 'catalog.db'; _build_minimal_catalog(catalog)
    marker = tmp_path / 'in-transaction'
    program = '''
import asyncio,hashlib,sys,time
from pathlib import Path
from knowledge_contracts import Principal
from knowledge_platform.local.package_import import LocalPackagePublisher
catalog,root,archive,marker=map(Path,sys.argv[1:])
publisher=LocalPackagePublisher(catalog,root)
def pause(connection):
    marker.write_text('uncommitted')
    time.sleep(60)
publisher._before_commit=pause
asyncio.run(publisher.import_package(Principal('admin',scopes=('knowledge.admin','knowledge.space:space_1')),archive,'sha256:'+hashlib.sha256(archive.read_bytes()).hexdigest()))
'''
    env = dict(os.environ)
    if os.environ.get('KNOWLEDGE_TEST_INSTALLED') == '1':
        env.pop('PYTHONPATH', None); env.pop('PYTHONHOME', None)
    else: env['PYTHONPATH'] = str(Path(__file__).parents[1])
    process = subprocess.Popen([sys.executable, '-c', program, str(catalog), str(tmp_path / 'processing'), str(archive), str(marker)],
                               cwd='/private/tmp', env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 10
        while not marker.exists() and process.poll() is None and time.monotonic() < deadline: time.sleep(.03)
        assert marker.exists(), process.communicate(timeout=1)
        process.kill(); process.wait(timeout=5)
    finally:
        if process.poll() is None: process.kill(); process.wait(timeout=5)
    with sqlite3.connect(catalog) as db:
        assert db.execute("SELECT COUNT(*) FROM knowledge_assets WHERE source_type='package'").fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM knowledge_package_imports').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM knowledge_package_chunks').fetchone()[0] == 0
    import asyncio
    from knowledge_contracts import Principal
    from knowledge_platform.local.package_import import LocalPackagePublisher
    with LocalPackagePublisher(catalog, tmp_path / 'processing') as publisher:
        result = asyncio.run(publisher.import_package(Principal('admin', scopes=('knowledge.admin', 'knowledge.space:space_1')),
            archive, 'sha256:' + hashlib.sha256(archive.read_bytes()).hexdigest()))
        assert result['idempotent'] is False
        assert publisher.read_published('asset_1') == b'# Package\nhello'
