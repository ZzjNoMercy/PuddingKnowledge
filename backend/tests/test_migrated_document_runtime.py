"""Real REST/MCP restart acceptance and collection isolation for migrated bodies."""
import asyncio
import base64
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.request import Request, urlopen

from test_document_migration import _run


def exercise_runtime(root, python, *, pythonpath=None, supervisor=False):
    _, source, files, candidate, body = _run(root)
    state = root / 'state'
    def call(port, path, payload=None):
        request = Request(f'http://127.0.0.1:{port}{path}',
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={'Content-Type': 'application/json'})
        with urlopen(request, timeout=5) as response:
            return json.load(response)
    for iteration in range(2):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0)); port = listener.getsockname()[1]
        ready = root / f'ready-{iteration}.json'
        command = [str(python), '-m', 'knowledge_platform.local', '--state-dir', str(state),
            '--temp-dir', str(root / f'temp-{iteration}'), '--ready-file', str(ready), '--port', str(port)]
        if iteration == 0: command += ['--document-migration', str(candidate)]
        environment = {'PATH': os.environ['PATH'], 'HOME': str(root)}
        if pythonpath: environment['PYTHONPATH'] = str(pythonpath)
        control = [str(python), '-m', 'knowledge_platform.local.supervisor']
        if supervisor:
            command = control + ['start', '--home', str(root/'home'), '--state-dir', str(state), '--port', str(port)]
            if iteration == 0: command += ['--document-migration', str(candidate)]
        with (root / f'server-{iteration}.log').open('wb') as log:
            if supervisor:
                started = subprocess.run(command, cwd=root, env=environment, capture_output=True, text=True, timeout=45)
                assert started.returncode == 0, started.stdout + started.stderr
                report = json.loads(started.stdout)
                assert report['active'] is True and report['health'] is True, report
                ready = next((root/'home/run').glob('*/ready.json'))
                child = None
            else:
                child = subprocess.Popen(command, cwd=root, env=environment, stdout=log, stderr=log)
            try:
                for _ in range(150):
                    assert child is None or child.poll() is None, (root / f'server-{iteration}.log').read_text()
                    if ready.exists():
                        try: assets = call(port, '/v1/assets?space_id=space_kb-1'); break
                        except OSError: pass
                    time.sleep(.04)
                else: raise AssertionError('runtime readiness timeout')
                assert json.loads(ready.read_text())['migrated_documents'] == 1
                asset = assets['data']['assets'][0]
                read = call(port, f"/v1/assets/{asset['id']}:read", {"start": 0, "end": len(body)})
                assert read['status'] == 'ok', json.dumps(read)
                assert base64.b64decode(read['data']['content_base64']) == body, read
                query = {'query': 'portable content', 'space_id': 'space_kb-1',
                    'collection_id': 'dataset_kb-1', 'capability_hint': 'document_rag_query'}
                result = call(port, '/v1/knowledge/query', query)
                assert result['status'] == 'ok' and result['evidence'], result
                assert result['evidence'][0]['asset_id'] == asset['id'], result
                direct = call(port, '/v1/document-rag/query', {'query': 'portable content', 'space_id': 'space_kb-1'})
                assert direct['status'] == 'ok' and direct['evidence'], direct
                mcp = call(port, '/mcp', {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                    'params': {'name': 'knowledge_query', 'arguments': query}})
                assert mcp['result']['structuredContent']['status'] == 'ok', mcp
            finally:
                if child is not None:
                    child.terminate(); child.wait(timeout=10)
                else:
                    stopped = subprocess.run(control + ['stop', '--home', str(root/'home')],
                        cwd=root, env=environment, capture_output=True, text=True, timeout=15)
                    assert stopped.returncode == 0, stopped.stdout + stopped.stderr
        assert not ready.exists()
        if iteration == 0:
            source.rename(root / 'removed-source'); files.rename(root / 'removed-files')
            candidate.rename(root / 'removed-candidate')
    return {'restarts': 1, 'rest_read': True, 'collection_query': True, 'direct_query': True,
        'mcp_query': True, 'original_sources_removed': True, 'activation_allowed': False}


def test_document_runtime_restart_without_original_sources(tmp_path):
    exercise_runtime(tmp_path, sys.executable, pythonpath=Path(__file__).parents[1])


def test_migrated_engine_cannot_read_another_collection(tmp_path):
    from knowledge_contracts import Principal, Correlation
    from knowledge_platform.catalog.sqlite_query import SqliteCatalogQueryRepository
    from knowledge_platform.local.app import _MigratedDocumentQueryEngine
    from knowledge_platform.router.ports import CollectionRoute, KnowledgeQueryRequest
    _, _, _, candidate, _ = _run(tmp_path)
    manifest = json.loads((candidate / 'manifest.json').read_text())
    bindings = {key: candidate / value for key, value in manifest['asset_bindings'].items()}
    engine = _MigratedDocumentQueryEngine(SqliteCatalogQueryRepository(candidate/'catalog.sqlite3'), bindings, None)
    principal = Principal('local', scopes=('knowledge.list', 'knowledge.search', 'knowledge.query', 'knowledge.read', 'knowledge.space:space_kb-1'))
    async def check(ids, binding='knowledge_migrated_documents'):
        return await engine.query(request=KnowledgeQueryRequest('portable content'),
            collection=CollectionRoute('other', 'space_kb-1', '1', ('document_rag_query',),
                asset_ids=ids, provider_bindings={'document_rag_query': {'provider_id': binding}}),
            principal=principal, correlation=Correlation('boundary'))
    for ids, provider in [((), 'knowledge_migrated_documents'), (('unbound',), 'knowledge_migrated_documents'),
                          (tuple(bindings), 'unregistered')]:
        result = asyncio.run(check(ids, provider))
        assert result.status == 'error' and not result.evidence


def test_same_space_collection_isolation_with_two_readable_documents(tmp_path):
    import sqlite3
    from knowledge_contracts import Principal, Correlation
    from knowledge_platform.catalog.sqlite_query import SqliteCatalogQueryRepository
    from knowledge_platform.local.app import _MigratedDocumentQueryEngine
    from knowledge_platform.router.ports import CollectionRoute, KnowledgeQueryRequest
    _, _, _, candidate, _ = _run(tmp_path)
    manifest = json.loads((candidate / 'manifest.json').read_text())
    bindings = {key: candidate / value for key, value in manifest['asset_bindings'].items()}
    original = next(iter(bindings))
    # Same Space, equally readable body, separate collection membership.
    with sqlite3.connect(candidate/'catalog.sqlite3') as db:
        db.row_factory = sqlite3.Row
        row = dict(db.execute('SELECT * FROM knowledge_assets').fetchone())
        row['id'] = 'other-document'
        row['source_uri'] = 'knowledge://spaces/space_kb-1/assets/other-document'
        db.execute('INSERT INTO knowledge_assets ('+','.join(row)+') VALUES ('+','.join('?' for _ in row)+')', tuple(row.values()))
    bindings['other-document'] = bindings[original]
    engine = _MigratedDocumentQueryEngine(SqliteCatalogQueryRepository(candidate/'catalog.sqlite3'), bindings, None)
    result = asyncio.run(engine.query(request=KnowledgeQueryRequest('portable content'),
        collection=CollectionRoute('selected', 'space_kb-1', '1', ('document_rag_query',), asset_ids=(original,),
            provider_bindings={'document_rag_query': {'provider_id': 'knowledge_migrated_documents'}}),
        principal=Principal('local', scopes=('knowledge.list', 'knowledge.search', 'knowledge.query', 'knowledge.read', 'knowledge.space:space_kb-1')),
        correlation=Correlation('boundary')))
    assert result.status == 'ok' and result.evidence, result
    assert {item.asset_id for item in result.evidence} == {original}


def test_document_supervisor_restart_without_original_sources(tmp_path):
    exercise_runtime(tmp_path, sys.executable, pythonpath=Path(__file__).parents[1], supervisor=True)
