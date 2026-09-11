import hashlib
import json
import os
import signal
import subprocess
import sys

import pytest
from test_migrate_from_claw_protocol import fixture
from knowledge_platform.distribution.migrate_from_claw import migrate_from_claw, REQUEST_FORMAT_V2


def wiki_fixture(root):
    request,output=fixture(root)
    wiki=root/'snapshot/brain'; (wiki/'raw').mkdir(parents=True)
    (wiki/'wiki').mkdir(); (wiki/'wiki/index.md').write_text('# Index')
    (wiki/'raw/data.md').write_bytes(b'raw')
    (wiki/'raw/manifest.jsonl').write_text(json.dumps({'snapshot_path':'data.md','sha256':hashlib.sha256(b'raw').hexdigest(),'size_bytes':3})+'\n')
    value=json.loads(request.read_text()); value.update(format=REQUEST_FORMAT_V2,source_wiki_root=str(wiki)); request.write_text(json.dumps(value))
    return request,output,wiki


def test_v2_receipt_preserves_wiki_evidence_without_claiming_runtime(tmp_path):
    request,output,wiki=wiki_fixture(tmp_path)
    result=migrate_from_claw(request,output,source_snapshot=tmp_path/'snapshot')
    assert result['format'].endswith('/v1')
    assert 'wiki_archive' in result['covered_domains'] and 'wiki' in result['pending_domains']
    assert 'wiki/archive/wiki/index.md' in result['artifacts']
    for path,digest in result['artifacts'].items(): assert 'sha256:'+hashlib.sha256((output/path).read_bytes()).hexdigest()==digest
    assert migrate_from_claw(request,output,source_snapshot=tmp_path/'snapshot')==result


@pytest.mark.parametrize('change',['escape','downgrade','missing','tamper','unknown'])
def test_archive_boundaries_and_completed_commitment(tmp_path,change):
    request,output,wiki=wiki_fixture(tmp_path)
    if change=='escape':
        data=json.loads(request.read_text()); data['source_wiki_root']=str(tmp_path);request.write_text(json.dumps(data))
    else:
        migrate_from_claw(request,output,source_snapshot=tmp_path/'snapshot')
        if change=='downgrade':
            data=json.loads(request.read_text());data['format']=data['format'].replace('/v2','/v1');data.pop('source_wiki_root');request.write_text(json.dumps(data))
        if change=='missing': (output/'wiki/archive/raw/data.md').unlink()
        if change=='tamper': (output/'wiki/archive/raw/data.md').write_bytes(b'bad')
        if change=='unknown': (output/'wiki/archive/unowned').write_bytes(b'keep')
    with pytest.raises((ValueError,OSError)): migrate_from_claw(request,output,source_snapshot=tmp_path/'snapshot')
    if change=='missing': assert not (output/'wiki/archive/raw/data.md').exists()


def test_sigkill_after_archive_before_receipt(tmp_path):
    request,output,wiki=wiki_fixture(tmp_path)
    code='''
import os,signal,sys
from knowledge_platform.distribution.migrate_from_claw import migrate_from_claw
migrate_from_claw(sys.argv[1],sys.argv[2],source_snapshot=sys.argv[3],_after_candidate=lambda:os.kill(os.getpid(),signal.SIGKILL))
'''
    result=subprocess.run([sys.executable,'-c',code,str(request),str(output),str(tmp_path/'snapshot')],capture_output=True)
    assert result.returncode == -signal.SIGKILL,result.stderr
    assert (output/'wiki/manifest.json').exists() and not (output/'receipt.json').exists()
    migrate_from_claw(request,output,source_snapshot=tmp_path/'snapshot')


def test_source_change_after_archive_refuses_receipt(tmp_path):
    request,output,wiki=wiki_fixture(tmp_path)
    with pytest.raises(ValueError):
        migrate_from_claw(request,output,source_snapshot=tmp_path/'snapshot',_after_candidate=lambda:(wiki/'wiki/index.md').write_text('changed'))
    assert not (output/'receipt.json').exists()
