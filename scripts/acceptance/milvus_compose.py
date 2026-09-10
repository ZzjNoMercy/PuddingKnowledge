#!/usr/bin/env python3
"""Opt-in Docker acceptance. Uses a fresh Home, project, credentials and ports.
Run: python3 scripts/acceptance/milvus_compose.py
Only the new project's containers/network are removed; its data and proof remain.
"""
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import tempfile
import time
import urllib.request


def main():
    asset = Path(__file__).resolve().parents[2] / 'packages/knowledge-platform-deploy-cli/assets/compose.platform.yml'
    home = Path(tempfile.mkdtemp(prefix='knowledge-milvus-proof-')).resolve()
    project = 'puddingknowledge-proof-' + secrets.token_hex(6)
    def free_port():
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            return sock.getsockname()[1]
    port, health_port = free_port(), free_port()
    while health_port == port:
        health_port = free_port()
    env = {k: v for k, v in os.environ.items() if not k.startswith(('PUDDINGKNOWLEDGE_', 'COMPOSE_'))}
    env.update({
        'PUDDINGKNOWLEDGE_HOME': str(home),
        'PUDDINGKNOWLEDGE_PROJECT_NAME': project,
        'PUDDINGKNOWLEDGE_MINIO_ROOT_USER': 'proof' + secrets.token_hex(8),
        'PUDDINGKNOWLEDGE_MINIO_ROOT_PASSWORD': secrets.token_hex(24),
        'PUDDINGKNOWLEDGE_POSTGRES_USER': 'proof',
        'PUDDINGKNOWLEDGE_POSTGRES_PASSWORD': secrets.token_hex(24),
        'PUDDINGKNOWLEDGE_MILVUS_PORT': str(port),
        'PUDDINGKNOWLEDGE_MILVUS_HTTP_PORT': str(health_port),
        **{f'PUDDINGKNOWLEDGE_{s}_IMAGE': 'unused/proof:never-started' for s in ('API', 'WORKER', 'CONSOLE')},
    })
    for part in ('postgres', 'milvus/etcd', 'milvus/minio', 'milvus/data'):
        (home / 'infrastructure' / part).mkdir(parents=True)
    base = ['docker', 'compose', '--env-file', os.devnull, '--project-name', project, '--file', str(asset)]
    def run(args, timeout=240):
        result = subprocess.run(args, env=env, capture_output=True, text=True, timeout=timeout)
        if result.returncode:
            raise RuntimeError(f'command failed ({result.returncode}): {args[:3]}: {result.stderr[-2000:]}')
        return result.stdout
    def compose(*args):
        return run(base + list(args))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def api(path, body):
        request = urllib.request.Request(f'http://127.0.0.1:{port}/v2/vectordb/{path}',
            data=json.dumps(body).encode(), headers={'Content-Type': 'application/json'})
        with opener.open(request, timeout=30) as response:
            result = json.load(response)
        if result.get('code') != 0:
            raise RuntimeError(f'Milvus {path}: {result}')
        return result.get('data')
    def search():
        result = api('entities/search', {'collectionName': 'proof_vectors', 'data': [[1, 0, 0, 0]],
            'limit': 2, 'outputFields': ['text'], 'consistencyLevel': 'Strong'})
        assert len(result) == 2 and str(result[0]['id']) == '101', result
        assert result[0]['text'] == 'persisted automobile', result
        return result
    before = set(run(['docker', 'ps', '-q']).split())
    proof = {'status': 'failed', 'project': project, 'home': str(home), 'activation_allowed': False,
        'compose_sha256': hashlib.sha256(asset.read_bytes()).hexdigest()}
    print(json.dumps({'stage': 'starting', 'home': str(home), 'project': project}), flush=True)
    try:
        config = json.loads(compose('config', '--format', 'json'))
        for name, service in config['services'].items():
            assert 'container_name' not in service, name
            assert all(p['host_ip'] == '127.0.0.1' for p in service.get('ports', [])), name
        milvus = config['services']['milvus']
        assert milvus['environment']['MINIO_SECRET_ACCESS_KEY'] == env['PUDDINGKNOWLEDGE_MINIO_ROOT_PASSWORD']
        assert milvus['environment']['MINIO_ACCESS_KEY_ID'] == env['PUDDINGKNOWLEDGE_MINIO_ROOT_USER']
        compose('up', '-d', '--pull', 'never', '--wait', '--wait-timeout', '180', 'milvus')
        api('collections/create', {'collectionName': 'proof_vectors', 'dimension': 4, 'metricType': 'COSINE',
            'params': {'consistencyLevel': 'Strong'}})
        api('entities/insert', {'collectionName': 'proof_vectors', 'data': [
            {'id': 101, 'vector': [1, 0, 0, 0], 'text': 'persisted automobile'},
            {'id': 102, 'vector': [0, 1, 0, 0], 'text': 'other topic'}]})
        proof['before_restart'] = search()
        supervisor = asset.with_name('platform-infra.sh')
        foreign_home = home / 'foreign-home'
        for part in ('postgres', 'milvus/etcd', 'milvus/minio', 'milvus/data'):
            (foreign_home / 'infrastructure' / part).mkdir(parents=True)
        rejected = subprocess.run([str(supervisor), 'down'], env=dict(env, PUDDINGKNOWLEDGE_HOME=str(foreign_home)),
            capture_output=True, text=True, timeout=30)
        assert rejected.returncode == 2 and 'not owned' in rejected.stderr, rejected.stderr
        search()
        proof['foreign_home_down_rejected'] = True
        print(json.dumps({'stage': 'recreating-containers'}), flush=True)
        run([str(supervisor), 'down'])
        compose('up', '-d', '--pull', 'never', '--wait', '--wait-timeout', '180', 'milvus')
        # A loaded collection may recover shortly after the process healthcheck.
        deadline = time.monotonic() + 60
        while True:
            try:
                proof['after_restart'] = search()
                break
            except Exception:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(2)
        proof['status'] = 'passed'
    finally:
        compose('down', '--timeout', '30')
        after = set(run(['docker', 'ps', '-q']).split())
        assert before <= after, 'A pre-existing container stopped during acceptance'
        assert not compose('ps', '-q').strip(), 'Test containers remain'
        proof['preexisting_containers_still_running'] = len(before)
        proof['test_containers_removed'] = True
        (home / 'proof.json').write_text(json.dumps(proof, indent=2) + '\n')
    print(json.dumps({'status': proof['status'], 'proof': str(home / 'proof.json')}), flush=True)


if __name__ == '__main__':
    main()
