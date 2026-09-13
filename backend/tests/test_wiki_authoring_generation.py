import json,sqlite3,threading
from concurrent.futures import ThreadPoolExecutor
import pytest
from knowledge_contracts import Principal
from knowledge_platform.local.wiki_authoring_admin import WikiAuthoringAdmin
from knowledge_platform.local.wiki_authoring_generation import TABLE
from test_wiki_authoring_admin import setup
from test_wiki_schema_publication import markdown


class Model:
    config={'endpoint':'http://127.0.0.1:1','model':'fixture'}
    def __init__(self,draft=None):self.calls=0;self.draft=draft
    def generate(self,context,instruction):
        self.calls+=1
        return self.draft if self.draft is not None else {'changes':[{'slug':'concepts/a','markdown':markdown('Alpha [[concepts/b]]')},{'slug':'concepts/b','markdown':markdown('Beta [[concepts/a]]')}],'index':'[[concepts/a]]\n[[concepts/b]]','log_entry':'Generated linked concepts.'}


def prepare(tmp_path,model=None):
    owned,store,api=setup(tmp_path)
    model=model or Model();admin=WikiAuthoringAdmin(store,evidence_root=owned['evidence_root'],model=model)
    who=Principal(subject_id='editor',scopes=('knowledge.admin',f'knowledge.space:{store.space_id}'))
    request={'space_id':store.space_id,'operation_id':'gen-one','expected_revision':store.read()[0],'slugs':[],'selected_raw':['source.md'],'instruction':'Compile selected evidence.'}
    return owned,store,admin,who,request,model


def test_generate_restart_retry_and_apply(tmp_path):
    owned,store,admin,who,request,model=prepare(tmp_path);before=store.read()
    result=admin.generate(who,request)
    assert result['state']=='ready' and model.calls==1 and store.read()==before
    patch=result['patch'];assert patch['expected_revision']==before[0] and patch['selected_raw']==['source.md']
    assert all(c['expected_digest'] is None for c in patch['changes'])
    restarted=WikiAuthoringAdmin(store,evidence_root=owned['evidence_root'])
    assert restarted.generate(who,request)==result
    assert restarted.proposal(who,{'space_id':store.space_id,'operation_id':'gen-one'})==result
    admin.apply(who,{'space_id':store.space_id,'operation_id':'publish-one','patch':patch})
    assert len(store.read()[1])==2 and model.calls==1
    with pytest.raises(ValueError,match='identity reused'):admin.generate(who,{**request,'instruction':'Different'})


@pytest.mark.parametrize('draft',[
 {'changes':[],'index':'','log_entry':'bad'},
 {'expected_revision':'fake','changes':[],'index':'','log_entry':'bad'},
 {'changes':[{'slug':'concepts/a','markdown':markdown(),'expected_digest':None}],'index':'[[concepts/a]]','log_entry':'bad'},
 {'changes':[{'slug':'concepts/a','markdown':markdown('[[concepts/missing]]')}],'index':'[[concepts/a]]','log_entry':'bad'}])
def test_invalid_model_result_is_durable_failure(tmp_path,draft):
    owned,store,admin,who,request,model=prepare(tmp_path,Model(draft));before=store.read()
    result=admin.generate(who,request)
    assert result['state']=='failed' and result['patch'] is None and store.read()==before
    assert admin.generate(who,request)==result and model.calls==1


def test_cannot_modify_unselected_existing_page(tmp_path):
    owned,store,admin,who,request,model=prepare(tmp_path)
    ready=admin.generate(who,request);admin.apply(who,{'space_id':store.space_id,'operation_id':'publish','patch':ready['patch']})
    before=store.read();request.update(operation_id='second',expected_revision=before[0])
    assert admin.generate(who,request)['state']=='failed' and store.read()==before
    request.update(operation_id='third',slugs=['concepts/a','concepts/b'])
    ready=admin.generate(who,request)
    assert ready['state']=='ready' and all(c['expected_digest'] for c in ready['patch']['changes'])


def test_concurrent_duplicate_generates_once(tmp_path):
    entered=threading.Event();release=threading.Event()
    class Blocking(Model):
        def generate(self,*args):entered.set();assert release.wait(5);return super().generate(*args)
    owned,store,admin,who,request,model=prepare(tmp_path,Blocking())
    with ThreadPoolExecutor(2) as pool:
        pending=pool.submit(admin.generate,who,request);assert entered.wait(5)
        duplicate=admin.generate(who,request)
        assert duplicate['state']=='unsettled' and duplicate['execution_liveness']=='unknown'
        release.set();assert pending.result()['state']=='ready'
    assert model.calls==1


def test_process_loss_marker_never_blindly_retries(tmp_path):
    class Lost(Model):
        def generate(self,*args):self.calls+=1;raise SystemExit('injected process loss')
    owned,store,admin,who,request,model=prepare(tmp_path,Lost())
    with pytest.raises(SystemExit):admin.generate(who,request)
    fresh=Model();restarted=WikiAuthoringAdmin(store,evidence_root=owned['evidence_root'],model=fresh)
    assert restarted.generate(who,request)['state']=='unsettled' and fresh.calls==0


def test_rejects_tampered_ready_record(tmp_path):
    owned,store,admin,who,request,model=prepare(tmp_path);admin.generate(who,request)
    with sqlite3.connect(store.database) as db:db.execute(f"UPDATE {TABLE} SET patch_json='{{}}'")
    with pytest.raises(ValueError,match='commitment'):admin.generate(who,request)
    assert model.calls==1


def test_permissions_and_stale_revision_precede_model(tmp_path):
    owned,store,admin,who,request,model=prepare(tmp_path)
    with pytest.raises(PermissionError):admin.generate(Principal(subject_id='x',scopes=('knowledge.admin',)),request)
    with pytest.raises(ValueError,match='Stale'):admin.generate(who,{**request,'expected_revision':'f'*64})
    assert model.calls==0


def test_sigkill_after_claim_preserves_unsettled_without_reexecution(tmp_path):
    import os,signal,multiprocessing
    class Killed(Model):
        def generate(self,*args):os.kill(os.getpid(),signal.SIGKILL)
    owned,store,admin,who,request,model=prepare(tmp_path,Killed());before=store.read()
    process=multiprocessing.get_context('fork').Process(target=lambda:admin.generate(who,request))
    process.start();process.join(10)
    if process.is_alive():process.kill();process.join();pytest.fail('generation child hung')
    assert process.exitcode==-signal.SIGKILL
    fresh=Model();restarted=WikiAuthoringAdmin(store,evidence_root=owned['evidence_root'],model=fresh)
    result=restarted.generate(who,request)
    assert result['state']=='unsettled' and result['automatic_retry'] is False
    assert fresh.calls==0 and store.read()==before
