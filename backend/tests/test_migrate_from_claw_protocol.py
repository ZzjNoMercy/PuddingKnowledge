import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from test_document_migration import _run
from knowledge_platform.distribution.migrate_from_claw import migrate_from_claw, REQUEST_FORMAT


def fixture(root):
    snapshot = root/"snapshot"; snapshot.mkdir()
    _, catalog, files, _, _ = _run(snapshot)
    request = root/'request.json'
    request.write_text(json.dumps({'format':REQUEST_FORMAT,'installation_id':'install-1','source_revision':'legacy-1',
        'source_schema_revision':'legacy-v1','source_catalog':str(catalog),'source_files_root':str(files),'bindings':{'doc-1':'docs/readme.md'}}))
    request.chmod(0o600)
    return request, root/'delegate'


def test_receipt_is_bound_inactive_and_resumable(tmp_path):
    request, output = fixture(tmp_path)
    result = migrate_from_claw(request, output, source_snapshot=tmp_path/'snapshot')
    assert result['request_digest'] == 'sha256:'+hashlib.sha256(request.read_bytes()).hexdigest()
    assert result['installation_prepared'] is False and result['pending_domains']
    assert not (output/'normalization').exists()
    assert str(tmp_path) not in json.dumps(result)
    for name, digest in result['artifacts'].items():
        assert 'sha256:'+hashlib.sha256((output/name).read_bytes()).hexdigest() == digest
    assert migrate_from_claw(request, output, source_snapshot=tmp_path/'snapshot') == result


def test_fail_after_candidate_resumes_and_holds_no_lock(tmp_path):
    request, output = fixture(tmp_path)
    def fail(): raise RuntimeError('interrupted')
    with pytest.raises(RuntimeError): migrate_from_claw(request, output, source_snapshot=tmp_path/'snapshot', _after_candidate=fail)
    assert not (output/'receipt.json').exists()
    assert migrate_from_claw(request, output, source_snapshot=tmp_path/'snapshot')['artifacts']


@pytest.mark.parametrize('change', ['request','receipt','blob','private','link','unknown'])
def test_refuses_changed_protocol_inputs_or_outputs(tmp_path, change):
    request, output = fixture(tmp_path)
    result = migrate_from_claw(request, output, source_snapshot=tmp_path/'snapshot')
    if change == 'request':
        data=json.loads(request.read_text());data['source_schema_revision']='changed';request.write_text(json.dumps(data))
    if change == 'receipt':
        data=json.loads((output/'receipt.json').read_text());data['activation_allowed']=0;(output/'receipt.json').write_text(json.dumps(data))
    if change == 'blob':
        name=next(n for n in result['artifacts'] if '/blobs/' in n);(output/name).write_bytes(b'changed')
    if change == 'private': request.chmod(0o644)
    if change == 'link': os.link(request,tmp_path/'request-alias')
    if change == 'unknown': (output/'unknown').mkdir()
    with pytest.raises(ValueError): migrate_from_claw(request, output, source_snapshot=tmp_path/'snapshot')


def test_cli_failure_is_redacted(tmp_path):
    request, output = fixture(tmp_path)
    request.write_text('{"secret":"must-not-appear"}')
    env={'PATH':os.environ['PATH'],'PYTHONPATH':str(Path(__file__).parents[1])}
    result=subprocess.run([sys.executable,'-m','knowledge_platform.distribution.migrate_from_claw','--source-snapshot',str(tmp_path/'snapshot'),'--request',str(request),'--output',str(output)],
        env=env,cwd=tmp_path,capture_output=True,text=True)
    assert result.returncode == 1
    assert 'must-not-appear' not in result.stdout+result.stderr and str(tmp_path) not in result.stdout+result.stderr


def test_actual_sigkill_between_candidate_and_receipt_is_resumable(tmp_path):
    import multiprocessing
    import signal
    import time
    request, output = fixture(tmp_path)
    context = multiprocessing.get_context('fork')
    reader, writer = context.Pipe(duplex=False)
    def target():
        def pause():
            writer.send('candidate_verified')
            time.sleep(60)
        migrate_from_claw(request, output, source_snapshot=tmp_path/'snapshot', _after_candidate=pause)
    child = context.Process(target=target)
    child.start()
    try:
        assert reader.poll(10) and reader.recv() == 'candidate_verified'
        os.kill(child.pid, signal.SIGKILL); child.join(timeout=5)
        assert child.exitcode == -signal.SIGKILL
    finally:
        if child.is_alive(): child.kill(); child.join(timeout=5)
        reader.close(); writer.close()
    assert (output/'candidate/manifest.json').exists() and not (output/'receipt.json').exists()
    assert migrate_from_claw(request, output, source_snapshot=tmp_path/'snapshot')['state'] == 'verified_inactive_partial'


def test_protocol_lock_and_relative_source_rejection(tmp_path):
    import fcntl
    request, output = fixture(tmp_path)
    migrate_from_claw(request, output, source_snapshot=tmp_path/'snapshot')
    with (output/'.migrate.lock').open('r+b') as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError): migrate_from_claw(request, output, source_snapshot=tmp_path/'snapshot')
    body = json.loads(request.read_text()); body['source_catalog'] = 'relative.sqlite3';request.write_text(json.dumps(body))
    with pytest.raises(ValueError): migrate_from_claw(request, tmp_path/'other', source_snapshot=tmp_path/'snapshot')


def test_protocol_publication_never_replaces_competing_final(tmp_path, monkeypatch):
    from knowledge_platform.distribution import migrate_from_claw as module
    target = tmp_path/'receipt.json'
    original = module.os.link
    def race(source, destination):
        target.write_bytes(b'competing'); target.chmod(0o600)
        original(source, destination)
    monkeypatch.setattr(module.os,'link',race)
    with pytest.raises(FileExistsError): module._write(target,b'new')
    assert target.read_bytes() == b'competing'


def test_interrupted_plan_link_pair_is_recovered(tmp_path, monkeypatch):
    from knowledge_platform.distribution import migrate_from_claw as module
    request, output = fixture(tmp_path)
    original = module.os.link
    def interrupt(source, destination):
        original(source, destination)
        if Path(destination).name == 'plan.json': raise RuntimeError('interrupted after link')
    with monkeypatch.context() as patch:
        patch.setattr(module.os,'link',interrupt)
        with pytest.raises(RuntimeError): migrate_from_claw(request, output, source_snapshot=tmp_path/'snapshot')
    assert (output/'plan.json').stat().st_nlink == 2
    assert migrate_from_claw(request, output, source_snapshot=tmp_path/'snapshot')['artifacts']
    assert (output/'plan.json').stat().st_nlink == 1


def test_request_cannot_select_another_snapshot(tmp_path):
    request, output = fixture(tmp_path)
    other = tmp_path/'other-installation';other.mkdir()
    with pytest.raises(ValueError, match='approved snapshot'):
        migrate_from_claw(request, output, source_snapshot=other)
    assert not output.exists()


def test_raw_catalog_wal_is_materialized_from_private_copy(tmp_path):
    request, output = fixture(tmp_path)
    catalog = tmp_path / 'snapshot' / 'legacy.sqlite3'
    writer = sqlite3.connect(catalog)
    try:
        assert writer.execute('PRAGMA journal_mode=WAL').fetchone()[0].lower() == 'wal'
        writer.execute("UPDATE knowledge_documents SET title='WAL title' WHERE id='doc-1'")
        writer.commit()
        wal = Path(str(catalog) + '-wal')
        assert wal.exists()
        before = {path.name: hashlib.sha256(path.read_bytes()).digest() for path in (catalog, wal, Path(str(catalog) + '-shm'))}
        result = migrate_from_claw(request, output, source_snapshot=tmp_path/'snapshot')
        assert result['activation_allowed'] is False
        assert 'source_catalog_bundle_digest' not in result
        assert 'normalization/report.json' in result['artifacts']
        assert json.loads((output/'normalization/report.json').read_text())['plan_digest'].startswith('sha256:')
        assert migrate_from_claw(request, output, source_snapshot=tmp_path/'snapshot') == result
        after = {path.name: hashlib.sha256(path.read_bytes()).digest() for path in (catalog, wal, Path(str(catalog) + '-shm'))}
        assert after == before
    finally:
        writer.close()
