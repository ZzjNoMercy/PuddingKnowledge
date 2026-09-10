"""Run the installed-capable rollback command from outside its source tree."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import hashlib


def test_cli_preserves_new_updated_deleted_rows_and_unowned_source(tmp_path):
    paths = [tmp_path / name for name in ('source.db','before.db','after.db')]
    for path in paths:
        with sqlite3.connect(path) as db:
            db.executescript('CREATE TABLE knowledge_assets(id TEXT PRIMARY KEY, body BLOB); CREATE INDEX body_index ON knowledge_assets(body);')
            db.executemany('INSERT INTO knowledge_assets VALUES (?,?)', [('update',b'old'),('delete',b'delete'),('keep',b'unchanged')])
    with sqlite3.connect(paths[0]) as db:
        db.executescript("CREATE TABLE sessions(id TEXT PRIMARY KEY, body TEXT); INSERT INTO sessions VALUES ('s','retain harness data');")
    with sqlite3.connect(paths[2]) as db:
        db.execute("UPDATE knowledge_assets SET body=? WHERE id='update'", (b'new content',))
        db.execute("DELETE FROM knowledge_assets WHERE id='delete'")
        db.execute("INSERT INTO knowledge_assets VALUES ('new', ?)", (b'new post-cutover data',))
    digests = [hashlib.sha256(path.read_bytes()).hexdigest() for path in paths]
    env = dict(os.environ)
    if os.environ.get('KNOWLEDGE_TEST_INSTALLED') == '1': env.pop('PYTHONPATH',None)
    else: env['PYTHONPATH'] = str(Path(__file__).parents[1])
    def run(output):
        return subprocess.run([sys.executable, '-m','knowledge_platform.distribution.rollback_cli',
            '--source-snapshot',str(paths[0]),'--target-before',str(paths[1]),'--target-after',str(paths[2]),
            '--output',str(output),'--table','knowledge_assets'], cwd='/private/tmp', env=env,
            capture_output=True,text=True,timeout=20)
    output = tmp_path/'candidate.db'; result=run(output)
    assert result.returncode == 0, result.stdout+result.stderr
    value=json.loads(result.stdout)
    assert value['tables']['knowledge_assets'] == {'insert':1,'update':1,'delete':1}
    assert value['installation_cutover_performed'] is False and value['activation_allowed'] is False
    assert value['writer_fence_verified'] is False
    assert all(secret not in result.stdout for secret in ('new content','retain harness data','post-cutover data',str(tmp_path)))
    with sqlite3.connect(output) as db:
        assert db.execute('SELECT id,body FROM knowledge_assets ORDER BY id').fetchall() == [('keep',b'unchanged'),('new',b'new post-cutover data'),('update',b'new content')]
        assert db.execute('SELECT body FROM sessions').fetchone()[0] == 'retain harness data'
    second=run(tmp_path/'candidate2.db');assert second.returncode==0, second.stdout
    assert json.loads(second.stdout)['output_digest'] == value['output_digest']
    conflict=run(output);assert conflict.returncode==1 and json.loads(conflict.stdout)['status']=='error'
    assert [hashlib.sha256(path.read_bytes()).hexdigest() for path in paths] == digests
