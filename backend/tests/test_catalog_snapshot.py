import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest
from knowledge_platform.distribution.catalog_snapshot import snapshot_catalog


def database(path, wal=False):
    db=sqlite3.connect(path, isolation_level=None)
    if wal:db.execute('PRAGMA journal_mode=WAL');db.execute('PRAGMA wal_autocheckpoint=0')
    db.execute('CREATE TABLE probe (id INTEGER PRIMARY KEY, value TEXT)')
    db.execute("INSERT INTO probe VALUES (1,'private-content')")
    return db


@pytest.mark.parametrize('wal',[False,True])
def test_real_writer_fence_consistent_backup_and_release(tmp_path,wal):
    source=tmp_path/'source.sqlite3';out=tmp_path/'snapshot.sqlite3';db=database(source,wal)
    def blocked():
        writer=sqlite3.connect(source,timeout=.01,isolation_level=None)
        try:
            with pytest.raises(sqlite3.OperationalError,match='locked'):writer.execute("INSERT INTO probe VALUES (2,'blocked')")
        finally:writer.close()
    try:
        result=snapshot_catalog(source,out,_while_fenced=blocked)
        assert result['catalog_fenced_during_snapshot'] and not result['writer_fence_held']
        assert result['catalog_schema_verified'] is False
        assert result['snapshot_digest']=='sha256:'+hashlib.sha256(out.read_bytes()).hexdigest()
        db.execute("INSERT INTO probe VALUES (2,'after release')")
        with sqlite3.connect(out) as snap:assert snap.execute('SELECT * FROM probe').fetchall()==[(1,'private-content')]
        assert out.stat().st_mode & 0o077==0
        assert not any(Path(str(out)+suffix).exists() for suffix in ['-wal','-shm','-journal'])
        with pytest.raises(ValueError):snapshot_catalog(source,out)
    finally:db.close()


def test_busy_writer_and_failure_publish_nothing(tmp_path):
    source=tmp_path/'source.sqlite3';out=tmp_path/'snapshot.sqlite3';db=database(source)
    try:
        db.execute('BEGIN IMMEDIATE')
        with pytest.raises(sqlite3.OperationalError):snapshot_catalog(source,out,timeout_seconds=.02)
        assert not out.exists();db.rollback()
        def fail():raise RuntimeError('injected interruption')
        with pytest.raises(RuntimeError):snapshot_catalog(source,out,_while_fenced=fail)
        assert not out.exists()
        db.execute("INSERT INTO probe VALUES (2,'lock released')")
        assert not list(tmp_path.glob('.knowledge-snapshot-*'))
    finally:db.close()


def test_replaced_source_refuses_publication(tmp_path):
    source=tmp_path/'source.sqlite3';out=tmp_path/'snapshot.sqlite3';db=database(source)
    replacement=tmp_path/'replacement';other=database(replacement);other.close()
    try:
        with pytest.raises(ValueError,match='fence lost'):
            snapshot_catalog(source,out,_while_fenced=lambda:os.replace(replacement,source))
        assert not out.exists()
    finally:db.close()


def test_links_rejected(tmp_path):
    source=tmp_path/'source';db=database(source);db.close();link=tmp_path/'link';link.symlink_to(source)
    with pytest.raises(ValueError):snapshot_catalog(link,tmp_path/'out')
    link.unlink();os.link(source,link)
    with pytest.raises(ValueError):snapshot_catalog(source,tmp_path/'out')


def test_cli_report_redacted(tmp_path):
    source=tmp_path/'private-name';db=database(source);db.close()
    env=dict(os.environ)
    if env.get('KNOWLEDGE_TEST_INSTALLED')=='1':env.pop('PYTHONPATH',None)
    else:env['PYTHONPATH']=str(Path(__file__).parents[1])
    p=subprocess.run([sys.executable,'-m','knowledge_platform.distribution.catalog_snapshot','--source',str(source),'--output',str(tmp_path/'out')],env=env,cwd=tmp_path,capture_output=True,text=True,check=True)
    report=json.loads(p.stdout);assert report['status']=='snapshot_created' and report['activation_allowed'] is False
    assert 'private' not in p.stdout and str(source) not in p.stdout


def test_killed_snapshot_releases_real_lock_without_publishing(tmp_path):
    import time
    source=tmp_path/'source';db=database(source);out=tmp_path/'out';marker=tmp_path/'fenced'
    env=dict(os.environ)
    if env.get('KNOWLEDGE_TEST_INSTALLED')=='1':env.pop('PYTHONPATH',None)
    else:env['PYTHONPATH']=str(Path(__file__).parents[1])
    code='''
import sys,time
from pathlib import Path
from knowledge_platform.distribution.catalog_snapshot import snapshot_catalog
def pause():
    Path(sys.argv[3]).write_text('fenced')
    time.sleep(30)
snapshot_catalog(sys.argv[1],sys.argv[2],_while_fenced=pause)
'''
    child=subprocess.Popen([sys.executable,'-c',code,str(source),str(out),str(marker)],env=env,cwd=tmp_path,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    try:
        deadline=time.monotonic()+10
        while not marker.exists() and child.poll() is None and time.monotonic()<deadline:time.sleep(.02)
        assert marker.exists()
        child.kill();child.wait(timeout=5)
        assert not out.exists()
        db.execute("INSERT INTO probe VALUES (2,'after process death')")
        snapshot_catalog(source,out)
        with sqlite3.connect(out) as snapshot:assert snapshot.execute('SELECT count(*) FROM probe').fetchone()==(2,)
    finally:
        if child.poll() is None:child.kill();child.wait(timeout=5)
        db.close()
