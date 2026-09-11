import json
import os
from pathlib import Path
import sqlite3

import pytest
from knowledge_platform.distribution import catalog_normalization as n


def fixture(tmp_path):
    source=tmp_path/'source';source.mkdir();catalog=source/'catalog.sqlite3'
    writer=sqlite3.connect(catalog);writer.execute('PRAGMA journal_mode=WAL');writer.execute('PRAGMA wal_autocheckpoint=0')
    writer.execute('CREATE TABLE evidence (value TEXT)');writer.execute("INSERT INTO evidence VALUES ('WAL value')");writer.commit()
    output=tmp_path/'normalization';output.mkdir(mode=0o700)
    return catalog,output,writer


def test_canonical_name_wal_recovery_and_replay(tmp_path):
    source,output,writer=fixture(tmp_path)
    try:
        before=n._facts(source)
        path,report=n.normalize_catalog(source,output)
        with sqlite3.connect(f'file:{path}?mode=ro',uri=True) as db:assert db.execute('SELECT value FROM evidence').fetchone()==('WAL value',)
        assert n.normalize_catalog(source,output)==(path,report)
        assert n._facts(source)==before
        assert not (output/'.work').exists()
        assert not any(Path(str(path)+s).exists() for s in ('-wal','-shm','-journal'))
    finally:writer.close()


@pytest.mark.parametrize('phase',['copied','materialized'])
def test_interrupted_normalization_resumes(tmp_path,phase):
    source,output,writer=fixture(tmp_path)
    try:
        def stop(current):
            if current==phase:raise RuntimeError('interrupted')
        with pytest.raises(RuntimeError):n.normalize_catalog(source,output,_after_phase=stop)
        assert not (output/'report.json').exists()
        assert n.normalize_catalog(source,output)[1]['state']=='verified'
    finally:writer.close()


@pytest.mark.parametrize('publication',['catalog','report'])
def test_link_publication_interruption_resumes(tmp_path,monkeypatch,publication):
    source,output,writer=fixture(tmp_path);original=n.os.link
    def crash(a,b):
        original(a,b)
        if Path(b).name==('catalog.sqlite3' if publication=='catalog' else 'report.json'):raise RuntimeError('crash after link')
    try:
        monkeypatch.setattr(n.os,'link',crash)
        with pytest.raises(RuntimeError):n.normalize_catalog(source,output)
        monkeypatch.setattr(n.os,'link',original)
        assert n.normalize_catalog(source,output)[1]['state']=='verified'
    finally:writer.close()


def test_source_change_after_materialization_never_commits(tmp_path):
    source,output,writer=fixture(tmp_path)
    try:
        def change(phase):
            if phase=='materialized':writer.execute("INSERT INTO evidence VALUES ('late value')");writer.commit()
        with pytest.raises(ValueError,match='changed'):n.normalize_catalog(source,output,_after_phase=change)
        assert not (output/'report.json').exists()
    finally:writer.close()


def test_copy_digest_mismatch_rejected(tmp_path,monkeypatch):
    source,output,writer=fixture(tmp_path);original=n._digest_file
    def mismatch(path,destination=None):
        fact=original(path,destination)
        if destination is not None:return {**fact,'digest':'sha256:'+'0'*64}
        return fact
    try:
        monkeypatch.setattr(n,'_digest_file',mismatch)
        with pytest.raises(ValueError,match='during copy'):n.normalize_catalog(source,output)
        assert not (output/'report.json').exists()
    finally:writer.close()


def test_unknown_work_is_not_deleted(tmp_path):
    source,output,writer=fixture(tmp_path)
    try:
        def stop(_):raise RuntimeError('stop')
        with pytest.raises(RuntimeError):n.normalize_catalog(source,output,_after_phase=stop)
        extra=output/'.work/unowned';extra.write_text('keep');extra.chmod(0o600)
        with pytest.raises(ValueError,match='Unowned'):n.normalize_catalog(source,output)
        assert extra.read_text()=='keep'
    finally:writer.close()


def test_completed_normalization_tamper_is_not_repaired(tmp_path):
    source,output,writer=fixture(tmp_path)
    try:
        path,_=n.normalize_catalog(source,output);path.unlink()
        before=(output/'report.json').read_bytes()
        with pytest.raises((ValueError,FileNotFoundError)):n.normalize_catalog(source,output)
        assert (output/'report.json').read_bytes()==before
    finally:writer.close()


def test_time_budget_and_source_directory_rejected(tmp_path,monkeypatch):
    source,output,writer=fixture(tmp_path)
    try:
        with pytest.raises(ValueError):n.normalize_catalog(source,source.parent)
        monkeypatch.setattr(n,'MAX_SECONDS',-1)
        with pytest.raises(ValueError,match='budget'):n.normalize_catalog(source,output)
        assert not (output/'report.json').exists()
    finally:writer.close()


def test_actual_sigkill_normalization_resume(tmp_path):
    import multiprocessing,signal,time
    source,output,writer=fixture(tmp_path)
    reader,send=multiprocessing.get_context('fork').Pipe(duplex=False)
    def run():
        def pause(phase):
            if phase=='materialized':send.send('ready');time.sleep(60)
        n.normalize_catalog(source,output,_after_phase=pause)
    proc=multiprocessing.get_context('fork').Process(target=run)
    try:
        proc.start();assert reader.poll(10);assert reader.recv()=='ready'
        proc.kill();proc.join(timeout=5);assert proc.exitcode==-signal.SIGKILL
        assert n.normalize_catalog(source,output)[1]['state']=='verified'
    finally:
        if proc.is_alive():proc.kill();proc.join(timeout=5)
        reader.close();send.close();writer.close()


def test_hot_rollback_journal_recovers_only_in_private_copy(tmp_path):
    import subprocess,sys,selectors
    source_dir=tmp_path/'source';source_dir.mkdir();source=source_dir/'catalog.sqlite3'
    with sqlite3.connect(source) as db:
        db.execute('CREATE TABLE evidence(id INTEGER PRIMARY KEY,value TEXT)')
        db.executemany('INSERT INTO evidence(value) VALUES (?)',[('original-'+('x'*2000),)]*400)
    code="""import sqlite3,sys
c=sqlite3.connect(sys.argv[1]);c.execute('PRAGMA cache_size=5');c.execute('PRAGMA journal_mode=DELETE');c.execute('BEGIN IMMEDIATE');c.execute("UPDATE evidence SET value='uncommitted-' || substr(value,1,1900)");print('ready',flush=True);input()
"""
    child=subprocess.Popen([sys.executable,'-c',code,str(source)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        sel=selectors.DefaultSelector();sel.register(child.stdout,selectors.EVENT_READ)
        assert sel.select(10);sel.close();assert child.stdout.readline().strip()=='ready'
        assert Path(str(source)+'-journal').stat().st_size>0
        child.kill();child.wait(timeout=5)
    finally:
        if child.poll() is None:child.kill()
        child.communicate(timeout=5)
    before=n._facts(source);output=tmp_path/'output';output.mkdir(mode=0o700)
    path,_=n.normalize_catalog(source,output)
    with sqlite3.connect(f'file:{path}?mode=ro',uri=True) as db:
        assert db.execute("SELECT count(*) FROM evidence WHERE value LIKE 'original-%'").fetchone()==(400,)
    assert n._facts(source)==before
