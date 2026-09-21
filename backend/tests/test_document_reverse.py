import hashlib
import json
import os
from pathlib import Path
import sqlite3

import pytest
from knowledge_platform.distribution.document_reverse import prepare_document_reverse
from knowledge_platform.catalog.rehearsal_runner import _digest
from test_core_catalog_reverse import fixture


def setup(tmp_path, *, new=False):
    source,before,after,_=fixture(tmp_path,real=True)
    bodies=tmp_path/'current-bodies';bodies.mkdir(mode=0o700)
    content=b'# Updated body\n';(bodies/'updated.md').write_bytes(content)
    with sqlite3.connect(after) as db:
        db.row_factory=sqlite3.Row
        original=dict(db.execute('SELECT * FROM knowledge_assets').fetchone())
        digest='sha256:'+hashlib.sha256(content).hexdigest()
        db.execute('UPDATE knowledge_assets SET content_digest=?,revision=?,title=?',(digest,digest,'Current title'))
        bindings={original['id']:'updated.md'}
        if new:
            native=dict(original,id='native-document-2',title='New native document',source_uri='knowledge://spaces/space_kb-1/assets/native-document-2',metadata_json='{"tag":"new"}')
            native['content_digest']=native['revision']='sha256:'+hashlib.sha256(b'new body').hexdigest()
            db.execute('INSERT INTO knowledge_assets ('+','.join(native)+') VALUES ('+','.join('?' for _ in native)+')',tuple(native.values()))
            (bodies/'new.md').write_bytes(b'new body');bindings[native['id']]='new.md'
            dataset=dict(db.execute('SELECT * FROM knowledge_datasets').fetchone());ids=json.loads(dataset['asset_ids'])+[native['id']]
            manifest=_digest({'asset_ids':ids,'source_revision':'legacy-1','version':dataset['version']})
            db.execute('UPDATE knowledge_datasets SET asset_ids=?,manifest_digest=?',(json.dumps(ids),manifest))
    return source,before,after,bodies,bindings,tmp_path/'reverse'


def run(args,**kwargs):return prepare_document_reverse(*args,source_revision='legacy-1',**kwargs)


def snapshots(args):return [p.read_bytes() for p in args[:3]]


def test_changed_and_new_documents_are_real_readable_legacy_files(tmp_path):
    args=setup(tmp_path,new=True);before=snapshots(args);receipt=run(args)
    assert receipt['document_count']==2 and not receipt['rollback_completed']
    with sqlite3.connect(args[-1]/'catalog.sqlite3') as db:
        db.row_factory=sqlite3.Row;rows=[dict(row) for row in db.execute('SELECT * FROM knowledge_documents')]
        assert len(rows)==2
        for row in rows:
            path=Path(row['storage_path']);body=path.read_bytes()
            assert path.is_relative_to(args[-1]/'bodies')
            assert hashlib.sha256(body).hexdigest()==row['content_sha256']
            assert len(body)==row['size_bytes']
        old=next(row for row in rows if row['id']=='doc-1')
        assert old['title']=='Current title' and json.loads(old['doc_metadata'])['nested']['token']=='private-value'
        assert old['publish_targets']=='["wiki"]'
        new=next(row for row in rows if row['id']==receipt['identity_map']['native-document-2'])
        assert new['title']=='New native document' and json.loads(new['doc_metadata'])['tag']=='new'
        assert new['publish_targets']=='[]' and new['source_connection_id'] is None
    assert snapshots(args)==before
    assert run(args)==dict(receipt,idempotent=True)


@pytest.mark.parametrize('mode',['body-missing','body-tamper','catalog-missing','catalog-tamper'])
def test_completed_missing_or_modified_artifacts_are_not_repaired(tmp_path,mode):
    args=setup(tmp_path);run(args);manifest=(args[-1]/'manifest.json').read_bytes()
    target=next((args[-1]/'bodies').iterdir()) if mode.startswith('body') else args[-1]/'catalog.sqlite3'
    if mode.endswith('missing'):target.unlink()
    else:target.write_bytes(b'changed')
    with pytest.raises((ValueError,OSError)):run(args)
    assert (args[-1]/'manifest.json').read_bytes()==manifest
    if mode.endswith('missing'):assert not target.exists()


@pytest.mark.parametrize('point',['body','catalog'])
def test_interruption_supports_exact_retry(tmp_path,point):
    args=setup(tmp_path,new=True)
    def fail(name):
        if (name=='catalog.sqlite3')==(point=='catalog'):raise RuntimeError('interrupted')
    with pytest.raises(RuntimeError):run(args,_after_copy=fail)
    assert json.loads((args[-1]/'manifest.json').read_text())['state']=='copying'
    assert run(args)['state']=='verified_inactive_documents'


@pytest.mark.parametrize('field,value',[
    ('source_uri','knowledge://spoof'),('permissions_json','{"allow":true}'),('description','unsupported'),
])
def test_unsupported_current_semantics_reject(tmp_path,field,value):
    args=setup(tmp_path)
    with sqlite3.connect(args[2]) as db:db.execute('UPDATE knowledge_assets SET '+field+'=?',(value,))
    with pytest.raises(ValueError):run(args)
    assert not (args[-1]/'catalog.sqlite3').exists()


def test_tampered_reference_provenance_is_not_hidden_by_rebinding(tmp_path):
    args=setup(tmp_path)
    with sqlite3.connect(args[2]) as db:
        metadata=json.loads(db.execute('SELECT metadata_json FROM knowledge_assets').fetchone()[0]);metadata['source_reference_digest']='sha256:spoof'
        db.execute('UPDATE knowledge_assets SET metadata_json=?',(json.dumps(metadata),))
    with pytest.raises(ValueError):run(args)


def test_body_digest_mismatch_rejects_before_output_creation(tmp_path):
    args=setup(tmp_path);(args[3]/'updated.md').write_bytes(b'wrong')
    with pytest.raises(ValueError):run(args)
    assert not args[-1].exists()


def test_virtual_root_rebinds_absolute_body_reference(tmp_path):
    args=setup(tmp_path)
    content=b'# Updated body\n\n![figure](/knowledge/assets/figure.png)\n'
    (args[3]/'updated.md').write_bytes(content)
    (args[3]/'resources/external/knowledge/assets').mkdir(parents=True)
    (args[3]/'resources/external/knowledge/assets/figure.png').write_bytes(b'figure')
    digest='sha256:'+hashlib.sha256(content).hexdigest()
    with sqlite3.connect(args[2]) as db:
        db.execute('UPDATE knowledge_assets SET content_digest=?,revision=?',(digest,digest))
    with pytest.raises(ValueError,match='Absolute document reference'):
        run(args)
    receipt=run(args,virtual_roots=[('/knowledge','resources/external/knowledge')])
    assert receipt['state']=='verified_inactive_documents'
    assert (args[-1]/'bodies/resources/external/knowledge/assets/figure.png').read_bytes()==b'figure'
    assert run(args,virtual_roots=[('/knowledge','resources/external/knowledge')])==dict(receipt,idempotent=True)


def test_body_change_during_copy_rejects_completion(tmp_path):
    args=setup(tmp_path)
    def mutate(name):
        if name!='catalog.sqlite3':(args[3]/'updated.md').write_bytes(b'late mutation')
    with pytest.raises(ValueError):run(args,_after_copy=mutate)
    assert json.loads((args[-1]/'manifest.json').read_text())['state']=='copying'


def test_relative_path_escape_rejects(tmp_path):
    args=list(setup(tmp_path));args[4]={next(iter(args[4])):'../escape'}
    with pytest.raises(ValueError):run(args)


def test_hardlinked_partial_is_not_truncated(tmp_path):
    from knowledge_platform.distribution.document_reverse import _copy
    source=tmp_path/'source';source.write_bytes(b'data')
    foreign=tmp_path/'foreign';foreign.write_bytes(b'must-survive');foreign.chmod(0o600)
    destination=tmp_path/'output';os.link(foreign,str(destination)+'.reverse-part')
    with pytest.raises(ValueError):_copy(source,destination,{'sha256':hashlib.sha256(b'data').hexdigest(),'size_bytes':4})
    assert foreign.read_bytes()==b'must-survive'


@pytest.mark.parametrize('point',['body','catalog'])
def test_sigkill_releases_output_lock_and_resumes(tmp_path,point):
    import subprocess,sys,time
    args=setup(tmp_path,new=True);ready=tmp_path/'ready'
    code='from knowledge_platform.distribution.document_reverse import prepare_document_reverse\nfrom pathlib import Path\nimport time\n'
    code+='def pause(name):\n if (name=="catalog.sqlite3")=='+repr(point=='catalog')+':\n  Path('+repr(str(ready))+').write_text(name)\n  while True:time.sleep(.02)\n'
    serialized=[str(arg) if isinstance(arg,Path) else arg for arg in args]
    code+='prepare_document_reverse(*'+repr(serialized)+',source_revision="legacy-1",_after_copy=pause)'
    env=dict(os.environ,PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    child=subprocess.Popen([sys.executable,'-c',code],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
    try:
        deadline=time.monotonic()+10
        while not ready.exists():
            if child.poll() is not None:raise AssertionError(child.stderr.read().decode())
            assert time.monotonic()<deadline;time.sleep(.02)
        child.kill();child.wait(timeout=5)
        assert run(args)['state']=='verified_inactive_documents'
    finally:
        if child.poll() is None:child.kill();child.wait(timeout=5)
        child.stderr.close()


def test_deleted_document_is_removed_from_catalog_and_identity_map(tmp_path):
    args=setup(tmp_path)
    with sqlite3.connect(args[2]) as db:
        version=db.execute('SELECT version FROM knowledge_datasets').fetchone()[0]
        db.execute('DELETE FROM knowledge_assets')
        db.execute('UPDATE knowledge_datasets SET asset_ids=?,manifest_digest=?',('[]',_digest({'asset_ids':[],'source_revision':'legacy-1','version':version})))
    args=list(args);args[4]={};receipt=run(args)
    assert receipt['identity_map']=={} and receipt['document_count']==0
    with sqlite3.connect(args[-1]/'catalog.sqlite3') as db:assert db.execute('SELECT * FROM knowledge_documents').fetchall()==[]


def test_inspection_and_committed_input_digest_bind_same_catalog(tmp_path,monkeypatch):
    from knowledge_platform.distribution import document_reverse as module
    args=setup(tmp_path);original=module._rows
    def replace_after_inspection(*values):
        result=original(*values)
        with sqlite3.connect(args[2]) as db:db.execute('UPDATE knowledge_assets SET title="changed after inspection"')
        return result
    monkeypatch.setattr(module,'_rows',replace_after_inspection)
    with pytest.raises(ValueError,match='after body inspection'):run(args)
    assert not args[-1].exists()


def test_completion_metadata_budget_does_not_publish_unreadable_manifest(tmp_path,monkeypatch):
    from knowledge_platform.distribution import document_reverse as module
    args=setup(tmp_path)
    def interrupt(name):raise RuntimeError('copy interrupted')
    with pytest.raises(RuntimeError):run(args,_after_copy=interrupt)
    marker=args[-1]/'manifest.json';copying=marker.read_bytes()
    with monkeypatch.context() as context:
        context.setattr(module.files,'MAX_JSON',len(copying)+1)
        with pytest.raises(ValueError,match='manifest exceeds budget'):run(args)
    assert marker.read_bytes()==copying
    assert run(args)['state']=='verified_inactive_documents'
