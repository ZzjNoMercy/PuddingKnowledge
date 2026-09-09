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
from urllib.request import urlopen

FIXTURE = """
import sqlite3, sys
from pathlib import Path
from sqlalchemy import create_engine
from knowledge_platform.catalog import migrate_to_latest
root=Path(sys.argv[1]); engine=create_engine(f"sqlite:///{root / 'catalog.sqlite3'}")
with engine.begin() as conn: migrate_to_latest(conn)
engine.dispose()
with sqlite3.connect(root / 'catalog.sqlite3') as conn:
 conn.execute("INSERT INTO knowledge_spaces VALUES ('space_kb_default','Local','','{}','now','now')")
 conn.execute("INSERT INTO knowledge_datasets VALUES ('dataset_kb_default','space_kb_default','Local','v1','document','','[]','[]','[]','{}','{}','','now','now')")
(root/'wiki').mkdir(); (root/'wiki/page.md').write_text('# Lifecycle fixture\\n\\nOWNED_SERVICE_EVIDENCE\\n')
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--fixture-python', type=Path, required=True)
    args = parser.parse_args()
    repo = args.repo.resolve()
    with tempfile.TemporaryDirectory(prefix='knowledge-service-') as directory:
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
        try:
            assert call(*start)['status'] == 'running'
            call(*start, fail=True)
            observed = call('status')
            assert observed['status'] == 'running', observed
            assert call('health')['status'] == 'running'
            with urlopen(f'http://127.0.0.1:{port}/v1/spaces', timeout=3) as response:
                assert json.load(response)['status'] == 'ok'
            assert call('stop', '--apply')['status'] == 'stopped'
            assert call('status')['status'] != 'running'
            assert call(*start)['status'] == 'running'
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
                          'source_catalog_unchanged': True, 'production_activation_allowed': False}))


if __name__ == '__main__':
    main()
