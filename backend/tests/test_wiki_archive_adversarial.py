import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

import pytest
from knowledge_platform.distribution import wiki_archive as archive


def fixture(root):
    root.mkdir()
    (root/'raw').mkdir()
    raw = b'a' * (2 * 1024 * 1024)
    (root/'raw/data.part').write_bytes(raw)
    (root/'raw/manifest.jsonl').write_text(json.dumps({'snapshot_path':'data.part', 'sha256':hashlib.sha256(raw).hexdigest(), 'size_bytes':len(raw)})+'\n')
    (root/'wiki').mkdir()
    (root/'wiki/page.md').write_text('# original')
    return root


@pytest.mark.parametrize('mode', ['partial_copy', 'payload_link', 'manifest_link'])
def test_actual_sigkill_inside_publication_resumes(tmp_path, mode):
    source = fixture(tmp_path/'source'); target=tmp_path/'target'
    code = '''
import os, signal, sys
from knowledge_platform.distribution import wiki_archive as m
mode=sys.argv[3]
real_write=os.write
real_link=os.link
def write(fd,data):
    if mode=='partial_copy' and len(data)>100000:
        real_write(fd,data[:100000]); os.kill(os.getpid(),signal.SIGKILL)
    return real_write(fd,data)
def link(src,dst,*a,**kw):
    result=real_link(src,dst,*a,**kw)
    if (mode=='payload_link' and str(src).endswith('.payload.part')) or (mode=='manifest_link' and str(dst).endswith('manifest.json')):
        os.kill(os.getpid(),signal.SIGKILL)
    return result
os.write=write; os.link=link
m.prepare_wiki_archive(sys.argv[1],sys.argv[2])
'''
    process=subprocess.run([sys.executable,'-c',code,str(source),str(target),mode],capture_output=True)
    assert process.returncode == -signal.SIGKILL, process.stderr
    archive.prepare_wiki_archive(source,target)
    assert (target/'archive/raw/data.part').read_bytes()==(source/'raw/data.part').read_bytes()
    assert not (target/'.payload.part').exists()
    source.rename(tmp_path/'disconnected')
    archive.verify_archive(target)


@pytest.mark.parametrize('mutation', ['plan', 'checkpoint', 'unknown', 'private', 'directory', 'missing_payload'])
def test_completed_archive_never_repairs_untrusted_state(tmp_path,mutation):
    source=fixture(tmp_path/'source'); target=tmp_path/'target'
    archive.prepare_wiki_archive(source,target)
    if mutation in {'plan','checkpoint'}: (target/(mutation+'.json')).write_text('{}')
    elif mutation=='unknown': (target/'unexpected').write_text('do not delete')
    elif mutation=='private': (target/'archive/wiki/page.md').chmod(0o644)
    elif mutation=='directory': (target/'archive/unknown').mkdir(mode=0o700)
    elif mutation=='missing_payload': (target/'archive/wiki/page.md').unlink()
    for call in (lambda:archive.verify_archive(target), lambda:archive.prepare_wiki_archive(source,target)):
        with pytest.raises((ValueError,OSError)): call()
    if mutation=='unknown': assert (target/'unexpected').read_text()=='do not delete'
    if mutation=='missing_payload': assert not (target/'archive/wiki/page.md').exists()


def test_source_identity_rebinding_rejected(tmp_path):
    source=fixture(tmp_path/'source'); target=tmp_path/'target'
    archive.prepare_wiki_archive(source,target)
    import shutil
    source.rename(tmp_path/'old'); shutil.copytree(tmp_path/'old',source)
    with pytest.raises(ValueError): archive.prepare_wiki_archive(source,target)
    archive.verify_archive(target)


def test_duplicate_json_key_and_directory_budget(tmp_path,monkeypatch):
    source=fixture(tmp_path/'source')
    manifest=source/'raw/manifest.jsonl'
    original=manifest.read_text()
    manifest.write_text(original.replace('"size_bytes":', '"size_bytes":0,"size_bytes":'))
    with pytest.raises(ValueError,match='Duplicate JSON'): archive.prepare_wiki_archive(source,tmp_path/'target')
    manifest.write_text(original)
    monkeypatch.setattr(archive,'MAX_FILES',2)
    with pytest.raises(ValueError,match='entry count'): archive.prepare_wiki_archive(source,tmp_path/'target')


def test_manifest_publication_pair_cannot_repair_missing_payload(tmp_path):
    source=fixture(tmp_path/'source'); target=tmp_path/'target'
    archive.prepare_wiki_archive(source,target)
    os.link(target/'manifest.json',target/'manifest.json.part')
    (target/'archive/raw/data.part').unlink()
    with pytest.raises(ValueError): archive.prepare_wiki_archive(source,target)
    assert not (target/'archive/raw/data.part').exists()


def test_checkpoint_boolean_is_not_integer(tmp_path):
    source=fixture(tmp_path/'source'); target=tmp_path/'target'
    archive.prepare_wiki_archive(source,target)
    path=target/'checkpoint.json'; content=json.loads(path.read_bytes())
    content['activation_allowed']=0
    path.write_bytes(archive._encode(content))
    with pytest.raises(ValueError): archive.verify_archive(target)


def test_verifier_honors_writer_lock(tmp_path):
    import fcntl
    source=fixture(tmp_path/'source'); target=tmp_path/'target'
    archive.prepare_wiki_archive(source,target)
    with (target/'.archive.lock').open('rb') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError): archive.verify_archive(target)
        with pytest.raises(BlockingIOError): archive.prepare_wiki_archive(source,target)


@pytest.mark.parametrize('relative', ['/tmp/escape', '../escape', 'a//b', './a', 'a/../b', 'a\\b'])
def test_raw_path_must_be_canonical(tmp_path,relative):
    source=fixture(tmp_path/'source'); path=source/'raw/manifest.jsonl'
    row=json.loads(path.read_text()); row['snapshot_path']=relative
    path.write_text(json.dumps(row)+'\n')
    with pytest.raises(ValueError): archive.prepare_wiki_archive(source,tmp_path/'target')
