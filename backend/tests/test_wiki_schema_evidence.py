import hashlib
import json
from pathlib import Path
import pytest
from knowledge_platform.distribution import wiki_archive as archive
from knowledge_platform.distribution.wiki_schema_evidence import capture_schema_evidence, verify_schema_evidence, CUSTOM, BRAIN
from knowledge_platform.local.workspace import open_persistent_workspace
from knowledge_platform.local.combined_workspace import bootstrap_combined_workspace, load_combined_workspace


def setup(tmp_path):
    fixture=json.loads((Path(__file__).parent/'fixtures/wiki-schema-legacy.json').read_text())
    brain=tmp_path/'brain';brain.mkdir()
    for rel,key in [(CUSTOM,'custom_yaml'),(BRAIN,'brain_yaml'),('AGENTS.md','agents_markdown')]:
        p=brain/rel;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(fixture[key])
    (brain/'wiki').mkdir();(brain/'wiki/concepts').mkdir();(brain/'wiki/concepts/page.md').write_text('# Page\n')
    (brain/'raw').mkdir();(brain/'raw/manifest.jsonl').write_text('')
    candidate=tmp_path/'archive';archive.prepare_wiki_archive(brain,candidate,installation_id='schema-test',source_revision='r1')
    resources=tmp_path/'resources';resources.mkdir();files={}
    for name,raw in fixture['catalog_yaml'].items():
        p=resources/(name+'.yaml');p.write_text(raw);files[name]=p
    data=capture_schema_evidence(candidate,files,expected_bundle_hash=fixture['expected_bundle_hash'])
    captured=tmp_path/'schema.json';captured.write_bytes(data);captured.chmod(0o600)
    return candidate,captured,files,fixture


def test_owned_schema_survives_source_disconnect_and_restart(tmp_path):
    candidate,captured,files,fixture=setup(tmp_path);state=tmp_path/'state'
    with open_persistent_workspace(state,wiki_archive=candidate,schema_evidence=captured) as owned:
        assert owned['schema_bundle'].bundle_hash==fixture['expected_bundle_hash']
    assert json.loads((state/'workspace.json').read_text())['version']==7
    for name in ['archive','schema.json','brain','resources']:(tmp_path/name).rename(tmp_path/(name+'-offline'))
    with open_persistent_workspace(state) as owned:
        assert owned['schema_bundle'].resolved_yaml==fixture['resolved_yaml']


@pytest.mark.parametrize('attack',['bytes','closure','archive_identity','archived_text','duplicate'])
def test_capture_tampering_rejected(tmp_path,attack):
    candidate,captured,files,fixture=setup(tmp_path);data=captured.read_bytes();value=json.loads(data)
    if attack=='bytes':
        with pytest.raises(ValueError):verify_schema_evidence(data+b' ',candidate,expected_digest=hashlib.sha256(data).hexdigest())
        return
    if attack=='closure':value['inputs']['catalog_yaml']['borrowed']+='\n'
    elif attack=='archive_identity':value['installation_id']='wrong'
    elif attack=='archived_text':value['inputs']['agents_markdown']+='tamper'
    elif attack=='duplicate':
        with pytest.raises(ValueError):verify_schema_evidence(data[:-1]+b',"format":"duplicate"}',candidate)
        return
    with pytest.raises((ValueError,RuntimeError)):verify_schema_evidence(json.dumps(value).encode(),candidate)


def test_owned_schema_digest_detects_replacement(tmp_path):
    candidate,captured,files,fixture=setup(tmp_path);state=tmp_path/'state'
    with open_persistent_workspace(state,wiki_archive=candidate,schema_evidence=captured):pass
    (state/'wiki-schema.json').write_bytes(captured.read_bytes()+b' ')
    with pytest.raises((ValueError,RuntimeError)):
        with open_persistent_workspace(state):pass


def test_external_resource_symlink_and_missing_pack_rejected(tmp_path):
    candidate,captured,files,fixture=setup(tmp_path)
    omitted=dict(files);omitted.pop('base')
    with pytest.raises((ValueError,RuntimeError)):capture_schema_evidence(candidate,omitted,expected_bundle_hash=fixture['expected_bundle_hash'])
    linked=tmp_path/'link.yaml';linked.symlink_to(files['base']);files['base']=linked
    with pytest.raises(ValueError):capture_schema_evidence(candidate,files,expected_bundle_hash=fixture['expected_bundle_hash'])


def test_combined_workspace_carries_schema(tmp_path):
    from test_document_migration import _run
    docs=tmp_path/'docs';docs.mkdir()
    _,_,_,document,_=_run(docs)
    candidate,captured,files,fixture=setup(tmp_path);state=tmp_path/'state'
    result=bootstrap_combined_workspace(document,candidate,state,schema_evidence=captured)
    assert result['schema_bundle'].bundle_hash==fixture['expected_bundle_hash']
    with open_persistent_workspace(state) as owned:
        assert owned['schema_bundle'].bundle_hash==fixture['expected_bundle_hash']
        assert owned['document_bindings'] and owned['wiki_bindings']


def test_null_owned_digest_cannot_disable_validation(tmp_path):
    candidate,captured,_,_=setup(tmp_path);state=tmp_path/'state'
    with open_persistent_workspace(state,wiki_archive=candidate,schema_evidence=captured):pass
    path=state/'workspace.json';manifest=json.loads(path.read_bytes());manifest['schema_evidence_digest']=None
    path.write_text(json.dumps(manifest))
    with pytest.raises(RuntimeError):
        with open_persistent_workspace(state):pass


def test_loader_keeps_archive_gate_during_binding_reads(tmp_path, monkeypatch):
    import fcntl, os
    from knowledge_platform.local import migrated_wiki
    candidate,captured,_,_=setup(tmp_path);state=tmp_path/'state'
    with open_persistent_workspace(state,wiki_archive=candidate,schema_evidence=captured):pass
    original=migrated_wiki._read;observed=[]
    def read(path,limit):
        if path.suffix=='.md':
            fd=os.open(state/'wiki-evidence/.archive.lock',os.O_RDWR)
            try:
                with pytest.raises(BlockingIOError):fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                observed.append(True)
            finally:os.close(fd)
        return original(path,limit)
    monkeypatch.setattr(migrated_wiki,'_read',read)
    with open_persistent_workspace(state):pass
    assert observed
