import hashlib,json,sqlite3
from pathlib import Path
from urllib.parse import unquote,urlsplit
import pytest
from test_core_catalog_reverse import fixture
from test_combined_workspace import _wiki
from knowledge_platform.distribution.document_migration import prepare_document_migration
from knowledge_platform.local.workspace import open_persistent_workspace,WorkspaceError
from knowledge_platform.local.combined_workspace import bootstrap_combined_workspace


def dependency_fixture(tmp_path):
    source,*_=fixture(tmp_path,real=True)
    root=tmp_path/'tree';root.mkdir();(root/'docs').mkdir();(root/'images').mkdir();(root/'images/empty').mkdir();(root/'files').mkdir()
    body=b'# Document\n\n![picture](../images/pic%23x.png)\n\n[linked](linked.md)\n'
    (root/'docs/main.md').write_bytes(body);(root/'docs/linked.md').write_text('[data](../files/data.csv)\n');(root/'images/pic#x.png').write_bytes(b'image bytes');(root/'files/data.csv').write_text('a,b\n1,2\n')
    with sqlite3.connect(source) as db:
        metadata=json.loads(db.execute('SELECT doc_metadata FROM knowledge_documents').fetchone()[0])
        metadata.update(assets=[{'path':'/old/images/pic#x.png','sha256':hashlib.sha256(b'image bytes').hexdigest(),'size_bytes':11}],multimodal={'image_assets_dir':'/old/images'})
        db.execute('UPDATE knowledge_documents SET storage_path=?,content_sha256=?,size_bytes=?,doc_metadata=?',('/old/docs/main.md',hashlib.sha256(body).hexdigest(),len(body),json.dumps(metadata)))
    attachments={'/old/images/pic#x.png':'images/pic#x.png','/old/images':'images'}
    return source,root,tmp_path/'candidate-tree',attachments,body


def run(args,**kw):return prepare_document_migration(args[0],args[1],{'doc-1':'docs/main.md'},args[2],attachment_bindings=args[3],**kw)


def assert_links(path):
    assert (path.parent/unquote(urlsplit('../images/pic%23x.png').path)).read_bytes()==b'image bytes'
    assert (path.parent/'linked.md').read_text()=='[data](../files/data.csv)\n'
    assert (path.parent/'../files/data.csv').read_text()=='a,b\n1,2\n'
    assert (path.parent/'../images/empty').is_dir()


@pytest.mark.parametrize('combined',[False,True])
def test_owned_relative_dependencies_survive_source_and_candidate_removal(tmp_path,combined):
    args=dependency_fixture(tmp_path);raw=args[0].read_bytes();run(args);assert run(args)['idempotent']
    manifest=json.loads((args[2]/'manifest.json').read_text());assert len(manifest['plan']['document_tree']['files'])==4
    state=tmp_path/'state'
    if combined:payload=bootstrap_combined_workspace(args[2],_wiki(tmp_path),state);assert_links(next(iter(payload['document_bindings'].values())))
    else:
        with open_persistent_workspace(state,document_migration=args[2]) as payload:assert_links(next(iter(payload['document_bindings'].values())))
    args[1].rename(tmp_path/'source-moved');args[2].rename(tmp_path/'candidate-moved')
    with open_persistent_workspace(state) as payload:
        path=next(iter(payload['document_bindings'].values()));assert path.read_bytes()==args[4];assert_links(path)
    assert args[0].read_bytes()==raw


@pytest.mark.parametrize('mode',['missing','extra','escape','digest','directory-conflict'])
def test_attachment_coverage_and_facts_reject(tmp_path,mode):
    args=list(dependency_fixture(tmp_path))
    if mode=='missing':args[3].pop('/old/images')
    elif mode=='extra':args[3]['/extra']='docs/main.md'
    elif mode=='escape':args[3]['/old/images']='../outside'
    elif mode=='directory-conflict':
        (args[1]/'other').mkdir();(args[1]/'other/pic#x.png').write_bytes(b'image bytes');args[3]['/old/images/pic#x.png']='other/pic#x.png'
    else:(args[1]/'images/pic#x.png').write_bytes(b'wrong')
    with pytest.raises(ValueError):run(args)
    assert not (args[2]/'manifest.json').exists()


@pytest.mark.parametrize('name',['images/pic#x.png','images/empty'])
def test_completed_resource_missing_is_not_repaired(tmp_path,name):
    args=dependency_fixture(tmp_path);run(args);marker=args[2]/'manifest.json';before=marker.read_bytes();target=args[2]/'resources'/name
    target.rmdir() if target.is_dir() else target.unlink()
    with pytest.raises(ValueError):run(args)
    assert not target.exists() and marker.read_bytes()==before


def test_interrupted_resource_publication_resumes(tmp_path):
    args=dependency_fixture(tmp_path)
    def stop(name):
        if name=='resources/docs/linked.md':raise RuntimeError('interrupted tree')
    with pytest.raises(RuntimeError):run(args,_after_publish=stop)
    assert not (args[2]/'manifest.json').exists()
    assert not run(args)['idempotent'];assert_links(args[2]/'resources/docs/main.md')


def test_added_source_directory_file_before_commit_rejects(tmp_path):
    args=dependency_fixture(tmp_path)
    def add(name):
        if name=='catalog.sqlite3':(args[1]/'images/new.png').write_bytes(b'new')
    with pytest.raises(ValueError,match='directory changed'):run(args,_after_publish=add)
    assert not (args[2]/'manifest.json').exists()


def test_owned_resource_tampering_rejects_restart(tmp_path):
    args=dependency_fixture(tmp_path);run(args);state=tmp_path/'state'
    with open_persistent_workspace(state,document_migration=args[2]):pass
    (state/'resources/images/pic#x.png').write_bytes(b'changed')
    with pytest.raises(WorkspaceError):
        with open_persistent_workspace(state):pass


def protocol_fixture(tmp_path):
    from knowledge_platform.distribution.migrate_from_claw import REQUEST_FORMAT
    snapshot=tmp_path/'snapshot';snapshot.mkdir();args=dependency_fixture(snapshot)
    request=tmp_path/'request.json';request.write_text(json.dumps({'format':REQUEST_FORMAT,'installation_id':'install-1','source_revision':'legacy-1','source_schema_revision':'legacy-v1','source_catalog':str(args[0]),'source_files_root':str(args[1]),'bindings':{'doc-1':'docs/main.md'},'attachment_bindings':args[3]}));request.chmod(0o600)
    return snapshot,args,request,tmp_path/'delegate'


def test_process_migration_receipt_covers_resources(tmp_path):
    from knowledge_platform.distribution.migrate_from_claw import migrate_from_claw
    snapshot,args,request,output=protocol_fixture(tmp_path)
    receipt=migrate_from_claw(request,output,source_snapshot=snapshot)
    assert 'candidate/resources/images/pic#x.png' in receipt['artifacts']
    assert_links(output/'candidate/resources/docs/main.md')
    assert migrate_from_claw(request,output,source_snapshot=snapshot)==receipt


def test_process_receipt_cannot_commit_missing_resource_directory(tmp_path):
    from knowledge_platform.distribution.migrate_from_claw import migrate_from_claw
    snapshot,args,request,output=protocol_fixture(tmp_path)
    def remove():(output/'candidate/resources/images/empty').rmdir()
    with pytest.raises(ValueError):migrate_from_claw(request,output,source_snapshot=snapshot,_after_candidate=remove)
    assert not (output/'receipt.json').exists()


@pytest.mark.parametrize('combined',[False,True])
def test_legacy_workspace_cannot_adopt_unregistered_resources(tmp_path,combined):
    from test_document_migration import _run
    import shutil
    _,_,_,candidate,_=_run(tmp_path)
    marker=candidate/'manifest.json';manifest=json.loads(marker.read_text());manifest['format']='puddingknowledge-document-migration/v1'
    for key in ('document_tree','tree_bindings','original_bindings'):manifest['plan'].pop(key)
    manifest['files']={k:v for k,v in manifest['files'].items() if not k.startswith('resources/')}
    shutil.rmtree(candidate/'resources');marker.write_text(json.dumps(manifest));state=tmp_path/'state'
    if combined:bootstrap_combined_workspace(candidate,_wiki(tmp_path),state)
    else:
        with open_persistent_workspace(state,document_migration=candidate):pass
    (state/'resources').mkdir(mode=0o700);(state/'resources/evil').write_bytes(b'unregistered')
    with pytest.raises(WorkspaceError):
        with open_persistent_workspace(state):pass
