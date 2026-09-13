import json,sqlite3,os,signal,multiprocessing
from dataclasses import replace
from pathlib import Path
import pytest
from knowledge_platform.wiki.patch import PageChange,WikiPatch,digest
from knowledge_platform.local.wiki_authoring import WikiAuthoringStore
from knowledge_platform.wiki.schema import admit_schema_bundle,schema_closure_sha256


def setup(tmp_path):
    f=json.loads((Path(__file__).parent/'fixtures/wiki-schema-legacy.json').read_text());f.pop('resolved_yaml');f.pop('legacy_source_sha256')
    inputs={k:v for k,v in f.items() if k!='expected_bundle_hash'}
    bundle=admit_schema_bundle(**f,expected_closure_sha256=schema_closure_sha256(**inputs))
    database=tmp_path/'catalog.sqlite3';database.touch(mode=0o600)
    store=WikiAuthoringStore(database=database,space_id='test',bundle=bundle,raw_hashes={'source.md':'a'*64},raw_manifest_sha256='b'*64)
    revision=store.initialize(pages={},index='# Index\n',log='# Original log\n')
    return store,revision


def page(body):
    return '---\ntitle: Concept\ntype: concept\nsources: [source.md]\ncreated: 2026-09-13\nupdated: 2026-09-13\nschema_version: 0.1.0\n---\n# Concept\n'+body


def patch(revision):
    return WikiPatch(revision,(PageChange('concepts/a',page('[[concepts/b]]'),None),PageChange('concepts/b',page('[[concepts/a]]'),None)),('source.md',),'# Index\n[[concepts/a]]\n[[concepts/b]]\n','Added two linked pages.')


def test_mutually_linked_pages_commit_together_and_retry(tmp_path):
    store,revision=setup(tmp_path);request=patch(revision)
    result=store.apply(request,operation_id='first');current=store.read()
    assert current[0]==result and set(current[1])=={'concepts/a','concepts/b'}
    assert current[3]=='# Original log\nAdded two linked pages.\n'
    assert store.apply(request,operation_id='first')==result
    assert store.read()==current
    with pytest.raises(ValueError,match='identity reused'):store.apply(replace(request,log_entry='Different'),operation_id='first')


def test_update_and_retirement_use_final_state_and_preserve_old_bytes(tmp_path):
    store,revision=setup(tmp_path);store.apply(patch(revision),operation_id='first')
    rev,pages,index,log=store.read()
    request=WikiPatch(rev,(PageChange('concepts/a',page('New standalone page'),digest(pages['concepts/a'])),PageChange('concepts/b',None,digest(pages['concepts/b']),'concepts/a')),('source.md',),'# Index\n[[concepts/a]]\n','Retired B into A.')
    store.apply(request,operation_id='second')
    _,final,_,newlog=store.read();assert set(final)=={'concepts/a'} and newlog.startswith(log)
    with sqlite3.connect(store.database) as db:
        retired=json.loads(db.execute("SELECT retired_json FROM knowledge_wiki_authoring_commits WHERE operation_id='second'").fetchone()[0])
    assert retired==[['concepts/b',pages['concepts/b'],'concepts/a']]


@pytest.mark.parametrize('attack',['stale_workspace','wrong_page','broken_retirement','duplicate','missing_replacement','wrong_source','bad_type'])
def test_invalid_patch_does_not_mutate_any_state(tmp_path,attack):
    store,rev=setup(tmp_path);store.apply(patch(rev),operation_id='first');before=store.read();rev,pages,index,log=before
    changes=(PageChange('concepts/a',page('Update [[concepts/b]]'),digest(pages['concepts/a'])),)
    request=WikiPatch(rev,changes,('source.md',),index,'Update.')
    if attack=='stale_workspace':request=replace(request,expected_revision='0'*64)
    elif attack=='wrong_page':request=replace(request,changes=(replace(changes[0],expected_digest='0'*64),))
    elif attack=='broken_retirement':request=replace(request,changes=(PageChange('concepts/b',None,digest(pages['concepts/b'])),),index='[[concepts/a]]')
    elif attack=='duplicate':request=replace(request,changes=changes+changes)
    elif attack=='missing_replacement':request=replace(request,changes=(PageChange('concepts/b',None,digest(pages['concepts/b']),'concepts/missing'),))
    elif attack=='wrong_source':request=replace(request,selected_raw=('missing.md',))
    elif attack=='bad_type':request=replace(request,changes=(replace(changes[0],markdown=page('Update').replace('type: concept','type: unknown')),))
    with pytest.raises(ValueError):store.apply(request,operation_id='bad')
    assert store.read()==before
    with sqlite3.connect(store.database) as db:assert db.execute('SELECT count(*) FROM knowledge_wiki_authoring_commits').fetchone()[0]==1


def test_sigkill_mid_patch_rolls_back_everything(tmp_path):
    store,revision=setup(tmp_path);before=store.read()
    def crash():
        store.apply(patch(revision),operation_id='crash',_after_page=lambda _:os.kill(os.getpid(),signal.SIGKILL))
    process=multiprocessing.get_context('fork').Process(target=crash);process.start();process.join(10)
    assert process.exitcode==-signal.SIGKILL
    assert store.read()==before
    assert store.apply(patch(revision),operation_id='crash')!=revision


def test_concurrent_patches_compare_workspace_revision(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    store,revision=setup(tmp_path)
    def apply(key):
        try:return store.apply(patch(revision),operation_id=key)
        except ValueError as error:return str(error)
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(apply,['one','two']))
    assert sum(r=='Stale Wiki workspace revision' for r in results)==1
    assert set(store.read()[1])=={'concepts/a','concepts/b'}


def test_invalid_initial_state_rejected_before_any_table_write(tmp_path):
    store,_=setup(tmp_path)
    other=WikiAuthoringStore(database=store.database,space_id='bad',bundle=store.bundle,raw_hashes=store.raw,raw_manifest_sha256=store.raw_manifest)
    with pytest.raises(ValueError,match='fails lint'):other.initialize(pages={'concepts/a':'invalid'},index='',log='')
    with sqlite3.connect(store.database) as db:assert db.execute("SELECT count(*) FROM knowledge_wiki_authoring_state WHERE space_id='bad'").fetchone()[0]==0


def test_retains_updated_before_image_and_rejects_corrupt_receipt(tmp_path):
    store,rev=setup(tmp_path);request=patch(rev);store.apply(request,operation_id='first')
    rev,pages,index,log=store.read()
    update=WikiPatch(rev,(PageChange('concepts/a',page('Updated [[concepts/b]]'),digest(pages['concepts/a'])),),('source.md',),index,'Update.')
    store.apply(update,operation_id='update')
    with sqlite3.connect(store.database) as db:
        before=json.loads(db.execute("SELECT before_json FROM knowledge_wiki_authoring_commits WHERE operation_id='update'").fetchone()[0])
        assert before==[['concepts/a',pages['concepts/a']]]
        db.execute("UPDATE knowledge_wiki_authoring_commits SET revision=? WHERE operation_id='update'",('0'*64,))
    with pytest.raises(ValueError,match='receipt mismatch'):store.apply(update,operation_id='update')


def test_owned_schema_workspace_can_seed_authoring_port(tmp_path):
    from test_wiki_schema_publication import prepare
    owned,state,config,request=prepare(tmp_path)
    store=WikiAuthoringStore.from_owned_workspace(owned)
    assert store.read()[1]=={}
    reopened=WikiAuthoringStore.from_owned_workspace(owned)
    assert reopened.read()==store.read()


@pytest.mark.asyncio
async def test_patch_authoring_fences_previous_single_page_writer(tmp_path):
    from test_wiki_schema_publication import prepare,Model,markdown
    from knowledge_platform.local.wiki import build_wiki_services
    owned,state,config,request=prepare(tmp_path)
    services=build_wiki_services(config,owned['catalog'],state/'processing',schema_workspace=owned)
    model=Model(request,markdown());services.wiki_compilation._model=model
    WikiAuthoringStore.from_owned_workspace(owned)
    with pytest.raises(ValueError,match='owned by transactional patch'):await services.wiki_compilation.compile(request)
    assert model.calls==0
