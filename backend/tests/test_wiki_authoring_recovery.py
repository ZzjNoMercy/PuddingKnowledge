import threading,sqlite3,json
from concurrent.futures import ThreadPoolExecutor
import pytest
from knowledge_contracts import Principal
from knowledge_platform.local.wiki_authoring_admin import WikiAuthoringAdmin
from knowledge_platform.local.wiki_authoring_generation import TABLE,RECOVERY
from test_wiki_authoring_generation import prepare,Model


def lost(tmp_path):
    class Lost(Model):
        def generate(self,*args):raise SystemExit('lost')
    owned,store,admin,who,request,model=prepare(tmp_path,Lost())
    with pytest.raises(SystemExit):admin.generate(who,request)
    proposal=admin.proposal(who,{'space_id':store.space_id,'operation_id':request['operation_id']})
    command={'space_id':store.space_id,'operation_id':request['operation_id'],'expected_receipt':proposal['receipt_digest'],'reason':'Operator revokes settlement authority after uncertain attempt.'}
    return owned,store,admin,who,request,command


def test_abandon_restart_exact_retry_and_explicit_new_generation(tmp_path):
    owned,store,admin,who,request,command=lost(tmp_path);before=store.read()
    result=admin.abandon(who,command)
    assert result['state']=='abandoned' and result['patch'] is None and store.read()==before
    fresh=Model();restarted=WikiAuthoringAdmin(store,evidence_root=owned['evidence_root'],model=fresh)
    assert restarted.abandon(who,command)==result
    assert restarted.generate(who,request)==result and fresh.calls==0
    with pytest.raises(ValueError,match='identity reused'):restarted.abandon(who,{**command,'reason':'Different'})
    assert restarted.generate(who,{**request,'operation_id':'explicit-new-attempt'})['state']=='ready' and fresh.calls==1


def test_late_model_result_cannot_resurrect_abandoned_attempt(tmp_path):
    entered=threading.Event();release=threading.Event()
    class Blocking(Model):
        def generate(self,*args):entered.set();assert release.wait(5);return super().generate(*args)
    owned,store,admin,who,request,model=prepare(tmp_path,Blocking());before=store.read()
    with ThreadPoolExecutor(2) as pool:
        running=pool.submit(admin.generate,who,request);assert entered.wait(5)
        receipt=admin.proposal(who,{'space_id':store.space_id,'operation_id':request['operation_id']})['receipt_digest']
        result=admin.abandon(who,{'space_id':store.space_id,'operation_id':request['operation_id'],'expected_receipt':receipt,'reason':'Revoke uncertain attempt.'})
        release.set();assert running.result()==result
    assert result['state']=='abandoned' and model.calls==1 and store.read()==before


def test_settlement_wins_then_abandon_refuses(tmp_path):
    entered=threading.Event();release=threading.Event()
    class Blocking(Model):
        def generate(self,*args):entered.set();assert release.wait(5);return super().generate(*args)
    owned,store,admin,who,request,model=prepare(tmp_path,Blocking())
    with ThreadPoolExecutor(2) as pool:
        running=pool.submit(admin.generate,who,request);assert entered.wait(5)
        pending=admin.proposal(who,{'space_id':store.space_id,'operation_id':request['operation_id']})
        release.set();result=running.result()
    with pytest.raises(ValueError,match='state or receipt changed'):admin.abandon(who,{'space_id':store.space_id,'operation_id':request['operation_id'],'expected_receipt':pending['receipt_digest'],'reason':'Too late'})
    assert admin.proposal(who,{'space_id':store.space_id,'operation_id':request['operation_id']})==result


@pytest.mark.parametrize('attack',['missing_scope','wrong_receipt','empty_reason'])
def test_recovery_rejections_leave_state_untouched(tmp_path,attack):
    owned,store,admin,who,request,command=lost(tmp_path)
    before=admin.proposal(who,{'space_id':store.space_id,'operation_id':request['operation_id']})
    if attack=='missing_scope':who=Principal(subject_id='x',scopes=('knowledge.admin',))
    if attack=='wrong_receipt':command['expected_receipt']='f'*64
    if attack=='empty_reason':command['reason']=''
    with pytest.raises((ValueError,PermissionError)):admin.abandon(who,command)
    with sqlite3.connect(store.database) as db:assert db.execute(f'SELECT state,receipt_digest FROM {TABLE}').fetchone()==('unsettled',before['receipt_digest'])


def test_recovery_event_and_state_are_atomic(tmp_path):
    owned,store,admin,who,request,command=lost(tmp_path)
    with sqlite3.connect(store.database) as db:db.execute(f"CREATE TRIGGER deny_abandon BEFORE UPDATE ON {TABLE} BEGIN SELECT RAISE(ABORT,'injected failure'); END")
    with pytest.raises(sqlite3.IntegrityError):admin.abandon(who,command)
    with sqlite3.connect(store.database) as db:
        assert db.execute(f'SELECT count(*) FROM {RECOVERY}').fetchone()[0]==0
        assert db.execute(f'SELECT state FROM {TABLE}').fetchone()[0]=='unsettled'


@pytest.mark.parametrize('attack',['delete','modify'])
def test_abandoned_record_requires_bound_recovery_event(tmp_path,attack):
    owned,store,admin,who,request,command=lost(tmp_path);admin.abandon(who,command)
    with sqlite3.connect(store.database) as db:
        if attack=='delete':db.execute(f'DELETE FROM {RECOVERY}')
        else:db.execute(f"UPDATE {RECOVERY} SET created_at='forged'")
    with pytest.raises(ValueError):admin.proposal(who,{'space_id':store.space_id,'operation_id':request['operation_id']})
