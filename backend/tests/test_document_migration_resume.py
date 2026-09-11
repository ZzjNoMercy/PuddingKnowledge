import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

import pytest
from test_document_migration import _legacy_catalog
from knowledge_platform.distribution.document_migration import prepare_document_migration
import hashlib


def fixture(root):
    catalog=root/'source.sqlite3';files=root/'files';files.mkdir();body=b'publication crash proof'
    (files/'doc.md').write_bytes(body);_legacy_catalog(catalog,content_digest=hashlib.sha256(body).hexdigest())
    return catalog,files,root/'output'


@pytest.mark.parametrize('point',['catalog.sqlite3','blob'])
def test_real_sigkill_publication_resumes(tmp_path,point):
    catalog,files,out=fixture(tmp_path);marker=tmp_path/'paused'
    env=dict(os.environ)
    if env.get('KNOWLEDGE_TEST_INSTALLED')=='1':env.pop('PYTHONPATH',None)
    else:env['PYTHONPATH']=str(Path(__file__).parents[1])
    code='''
import sys,time
from pathlib import Path
from knowledge_platform.distribution.document_migration import prepare_document_migration
def pause(name):
    if name==sys.argv[5] or (sys.argv[5]=='blob' and name.startswith('blobs/')):
        Path(sys.argv[4]).write_text('paused');time.sleep(30)
prepare_document_migration(sys.argv[1],sys.argv[2],{'doc-1':'doc.md'},sys.argv[3],_after_publish=pause)
'''
    child=subprocess.Popen([sys.executable,'-c',code,str(catalog),str(files),str(out),str(marker),point],env=env,cwd=tmp_path,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    try:
        deadline=time.monotonic()+10
        while not marker.exists() and child.poll() is None and time.monotonic()<deadline:time.sleep(.02)
        assert marker.exists()
        with pytest.raises(BlockingIOError):prepare_document_migration(catalog,files,{'doc-1':'doc.md'},out)
        child.kill();child.wait(timeout=5)
        assert not (out/'manifest.json').exists()
        assert json.loads((out/'checkpoint.json').read_text())['state']=='copying'
        assert prepare_document_migration(catalog,files,{'doc-1':'doc.md'},out)['idempotent'] is False
        assert prepare_document_migration(catalog,files,{'doc-1':'doc.md'},out)['idempotent'] is True
    finally:
        if child.poll() is None:child.kill();child.wait(timeout=5)


@pytest.mark.parametrize('change',['source','checkpoint','catalog','partial'])
def test_partial_tampering_cannot_resume(tmp_path,change):
    catalog,files,out=fixture(tmp_path)
    def stop(name):raise RuntimeError('stop')
    with pytest.raises(RuntimeError):prepare_document_migration(catalog,files,{'doc-1':'doc.md'},out,_after_publish=stop)
    if change=='source':(files/'doc.md').write_bytes(b'changed')
    elif change=='checkpoint':
        data=json.loads((out/'checkpoint.json').read_text());data['activation_allowed']=True;(out/'checkpoint.json').write_text(json.dumps(data))
    elif change=='catalog':
        with sqlite3.connect(out/'catalog.sqlite3') as db:db.execute("UPDATE knowledge_assets SET title='changed'")
    else:
        (out/'catalog.sqlite3.migration-part').symlink_to(catalog)
    with pytest.raises(ValueError):prepare_document_migration(catalog,files,{'doc-1':'doc.md'},out)
    assert not (out/'manifest.json').exists()


def test_late_catalog_mutation_does_not_get_a_valid_final_digest(tmp_path):
    catalog,files,out=fixture(tmp_path)
    def mutate(name):
        if name.startswith('blobs/'):
            with sqlite3.connect(out/'catalog.sqlite3') as db:db.execute("UPDATE knowledge_assets SET title='late mutation'")
    with pytest.raises(ValueError):prepare_document_migration(catalog,files,{'doc-1':'doc.md'},out,_after_publish=mutate)
    assert not (out/'manifest.json').exists()


def test_legacy_complete_manifest_without_checkpoint_still_verifies(tmp_path):
    catalog,files,out=fixture(tmp_path)
    prepare_document_migration(catalog,files,{'doc-1':'doc.md'},out)
    (out/'checkpoint.json').unlink();(out/'.migration.lock').unlink()
    assert prepare_document_migration(catalog,files,{'doc-1':'doc.md'},out)['idempotent'] is True


def test_incomplete_part_is_replaced_after_checkpoint_verification(tmp_path):
    catalog,files,out=fixture(tmp_path)
    def stop(name):raise RuntimeError('stop after catalog')
    with pytest.raises(RuntimeError):prepare_document_migration(catalog,files,{'doc-1':'doc.md'},out,_after_publish=stop)
    plan=json.loads((out/'checkpoint.json').read_text());name=next(iter(plan['blob_digests']))
    part=out/(name+'.migration-part');part.parent.mkdir(mode=0o700);part.write_bytes(b'truncated');part.chmod(0o600)
    assert prepare_document_migration(catalog,files,{'doc-1':'doc.md'},out)['idempotent'] is False
    assert not part.exists()


def test_atomic_publication_does_not_replace_racing_file(tmp_path,monkeypatch):
    import knowledge_platform.distribution.document_migration as module
    catalog,files,out=fixture(tmp_path);original=module.os.link
    def racing_link(source,destination,*args,**kwargs):
        if Path(destination).name=='catalog.sqlite3':
            Path(destination).write_bytes(b'foreign file');Path(destination).chmod(0o600)
        return original(source,destination,*args,**kwargs)
    monkeypatch.setattr(module.os,'link',racing_link)
    with pytest.raises(FileExistsError):prepare_document_migration(catalog,files,{'doc-1':'doc.md'},out)
    assert (out/'catalog.sqlite3').read_bytes()==b'foreign file'
    assert not (out/'manifest.json').exists()


def test_interrupted_no_replace_link_pair_is_recovered(tmp_path,monkeypatch):
    import knowledge_platform.distribution.document_migration as module
    catalog,files,out=fixture(tmp_path);original=module.os.link
    def linked_then_stopped(source,destination,*args,**kwargs):
        original(source,destination,*args,**kwargs)
        if Path(destination).name=='catalog.sqlite3':raise RuntimeError('killed after link')
    monkeypatch.setattr(module.os,'link',linked_then_stopped)
    with pytest.raises(RuntimeError):prepare_document_migration(catalog,files,{'doc-1':'doc.md'},out)
    assert (out/'catalog.sqlite3').stat().st_nlink==2
    monkeypatch.setattr(module.os,'link',original)
    assert prepare_document_migration(catalog,files,{'doc-1':'doc.md'},out)['idempotent'] is False
    assert (out/'catalog.sqlite3').stat().st_nlink==1
    assert not (out/'catalog.sqlite3.migration-part').exists()
