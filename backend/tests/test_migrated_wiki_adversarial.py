import hashlib,json,sqlite3
from pathlib import Path
import pytest
from knowledge_platform.distribution.wiki_archive import prepare_wiki_archive
from knowledge_platform.local.workspace import open_persistent_workspace,WorkspaceError


def make(root):
    brain=root/'brain';(brain/'raw').mkdir(parents=True);(brain/'wiki').mkdir()
    (brain/'raw/a.md').write_bytes(b'a')
    (brain/'raw/manifest.jsonl').write_text(json.dumps({'snapshot_path':'a.md','sha256':hashlib.sha256(b'a').hexdigest(),'size_bytes':1})+'\n')
    (brain/'wiki/a.md').write_text('---\ntitle: Page A\n---\n# Original evidence\n')
    (brain/'wiki/index.md').write_text('index history')
    candidate=root/'candidate';prepare_wiki_archive(brain,candidate,installation_id='stable-installation')
    return brain,candidate,root/'state'


def test_stable_identity_across_archive_revision_and_location(tmp_path):
    brain,candidate,state=make(tmp_path)
    with open_persistent_workspace(state,wiki_archive=candidate) as first: ids=set(first['file_bindings']);spaces=first['space_ids']
    (brain/'wiki/a.md').write_text('# Revised same page\n')
    second=tmp_path/'second-candidate';prepare_wiki_archive(brain,second,installation_id='stable-installation',source_revision='legacy-2')
    with open_persistent_workspace(tmp_path/'second-state',wiki_archive=second) as other:
        assert set(other['file_bindings'])==ids and other['space_ids']==spaces


@pytest.mark.parametrize('change',['source_type','source_uri','collection_assets'])
def test_catalog_and_manifest_facts_cannot_jointly_forge_binding(tmp_path,change):
    brain,candidate,state=make(tmp_path)
    with open_persistent_workspace(state,wiki_archive=candidate):pass
    manifest=json.loads((state/'workspace.json').read_text());asset=next(iter(manifest['facts']['assets']))
    with sqlite3.connect(state/'catalog.sqlite3') as db:
        if change in {'source_type','source_uri'}:
            value='wrong';db.execute('UPDATE knowledge_assets SET '+change+'=? WHERE id=?',(value,asset));manifest['facts']['assets'][asset][change]=value
        else:
            ids=[asset,asset];db.execute('UPDATE knowledge_datasets SET asset_ids=?',(json.dumps(ids),));manifest['facts']['collection']['asset_ids']=ids
    (state/'workspace.json').write_text(json.dumps(manifest))
    with pytest.raises(WorkspaceError):open_persistent_workspace(state)


@pytest.mark.parametrize('pages',[0,True,'1'])
def test_page_count_cannot_override_archive(tmp_path,pages):
    _,candidate,state=make(tmp_path)
    with open_persistent_workspace(state,wiki_archive=candidate):pass
    path=state/'workspace.json';manifest=json.loads(path.read_text());manifest['pages']=pages;path.write_text(json.dumps(manifest))
    with pytest.raises(WorkspaceError):open_persistent_workspace(state)


def test_actual_kill_before_complete_marker_is_fail_closed(tmp_path):
    import os,signal,subprocess,sys
    _,candidate,state=make(tmp_path)
    code='''
import os,sys,signal
from pathlib import Path
from knowledge_platform.local.workspace import open_persistent_workspace
real=os.replace
def replace(source,target):
 result=real(source,target)
 if Path(target).name=='workspace.json':os.kill(os.getpid(),signal.SIGKILL)
 return result
os.replace=replace
open_persistent_workspace(sys.argv[2],wiki_archive=Path(sys.argv[1]))
'''
    result=subprocess.run([sys.executable,'-c',code,str(candidate),str(state)],capture_output=True)
    assert result.returncode == -signal.SIGKILL,result.stderr
    assert (state/'.initializing').exists()
    with pytest.raises(WorkspaceError):open_persistent_workspace(state)
