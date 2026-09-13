import sqlite3,time,threading
from concurrent.futures import ThreadPoolExecutor
import pytest
from knowledge_contracts import Principal
from knowledge_platform.local.wiki_authoring_admin import WikiAuthoringAdmin
from knowledge_platform.local.wiki_authoring_queue import TABLE
from knowledge_platform.local.wiki_queue_control import EVENTS
from test_wiki_authoring_generation import prepare,Model


def setup(tmp_path):
    owned,store,admin,who,request,model=prepare(tmp_path)
    admission={'request':request,'not_before':0};queued=admin.enqueue(who,admission)
    return owned,store,admin,who,request,model,admission,queued


def command(request,queued,action='cancel',key='change-one',**extra):
    return {'space_id':request['space_id'],'operation_id':request['operation_id'],'command_id':key,'expected_receipt':queued.get('admission_receipt',queued.get('result_receipt')),'reason':'Operator changes scheduling intent.','action':action,**extra}


def test_cancel_is_durable_and_prevents_all_claim_paths(tmp_path):
    owned,store,admin,who,request,model,admission,queued=setup(tmp_path)
    body=command(request,queued);result=admin.control_queue(who,body)
    assert result['state']=='cancelled'
    assert admin.control_queue(who,body)==result
    restarted=WikiAuthoringAdmin(store,evidence_root=owned['evidence_root'],model=model)
    assert restarted.enqueue(who,admission)['state']=='cancelled'
    assert restarted.control_queue(who,body)==result
    assert restarted.run_queue(who,{'space_id':store.space_id})['processed'] is None
    with pytest.raises(ValueError,match='admission'):restarted.generate(who,request)
    assert model.calls==0


def test_reschedule_and_historical_command_retry_preserve_original_admission(tmp_path):
    owned,store,admin,who,request,model,admission,queued=setup(tmp_path)
    future=int(time.time())+100
    body=command(request,queued,'reschedule',not_before=future)
    first=admin.control_queue(who,body)
    assert admin.enqueue(who,admission)['not_before']==future
    with pytest.raises(ValueError):admin.enqueue(who,{'request':request,'not_before':future})
    assert admin.run_queue(who,{'space_id':store.space_id})['processed'] is None
    second=admin.control_queue(who,command(request,first,'reschedule','change-two',not_before=0))
    assert admin.control_queue(who,body)==first
    assert admin.queue(who,{'space_id':store.space_id})['items'][0]['admission_receipt']==second['result_receipt']
    assert admin.run_queue(who,{'space_id':store.space_id})['processed']['state']=='ready' and model.calls==1


def test_cancel_wins_against_worker_selected_before_claim(tmp_path,monkeypatch):
    owned,store,admin,who,request,model,admission,queued=setup(tmp_path)
    entered=threading.Event();release=threading.Event();generate=admin.generate
    def delayed(*args):entered.set();assert release.wait(5);return generate(*args)
    monkeypatch.setattr(admin,'generate',delayed)
    with ThreadPoolExecutor(1) as pool:
        running=pool.submit(admin.run_queue,who,{'space_id':store.space_id});assert entered.wait(5)
        admin.control_queue(who,command(request,queued));release.set()
        assert running.result()['processed']['state']=='cancelled'
    assert model.calls==0


def test_claim_wins_control_refuses(tmp_path):
    entered=threading.Event();release=threading.Event()
    class Blocking(Model):
        def generate(self,*args):entered.set();assert release.wait(5);return super().generate(*args)
    owned,store,admin,who,request,model=prepare(tmp_path,Blocking())
    queued=admin.enqueue(who,{'request':request,'not_before':0})
    with ThreadPoolExecutor(1) as pool:
        running=pool.submit(admin.run_queue,who,{'space_id':store.space_id});assert entered.wait(5)
        try:
            with pytest.raises(ValueError,match='claimed'):admin.control_queue(who,command(request,queued))
        finally:release.set()
        assert running.result()['processed']['state']=='ready'


@pytest.mark.parametrize('attack',['actor','receipt','changed-command','reason','boolean-time'])
def test_control_rejections(tmp_path,attack):
    owned,store,admin,who,request,model,admission,queued=setup(tmp_path);body=command(request,queued)
    if attack=='actor':who=Principal(subject_id='other',scopes=who.scopes)
    if attack=='receipt':body['expected_receipt']='f'*64
    if attack=='reason':body['reason']=''
    if attack=='boolean-time':body.update(action='reschedule',not_before=True)
    if attack=='changed-command':admin.control_queue(who,body);body['reason']='Different'
    with pytest.raises((PermissionError,ValueError)):admin.control_queue(who,body)
    assert model.calls==0


@pytest.mark.parametrize('attack',['delete','modify','head'])
def test_control_chain_corruption_rejected(tmp_path,attack):
    owned,store,admin,who,request,model,admission,queued=setup(tmp_path)
    admin.control_queue(who,command(request,queued,'reschedule',not_before=0))
    with sqlite3.connect(store.database) as db:
        if attack=='delete':db.execute(f'DELETE FROM {EVENTS}')
        elif attack=='modify':db.execute(f"UPDATE {EVENTS} SET created_at='forged'")
        else:db.execute(f"UPDATE {TABLE} SET history_digest=''")
    with pytest.raises(ValueError):admin.queue(who,{'space_id':store.space_id})
    with pytest.raises(ValueError):admin.run_queue(who,{'space_id':store.space_id})
    assert model.calls==0


def test_control_transaction_failure_leaves_no_event(tmp_path):
    owned,store,admin,who,request,model,admission,queued=setup(tmp_path)
    with sqlite3.connect(store.database) as db:db.execute(f"CREATE TRIGGER reject_control BEFORE UPDATE ON {TABLE} BEGIN SELECT RAISE(ABORT,'failure'); END")
    with pytest.raises(sqlite3.IntegrityError):admin.control_queue(who,command(request,queued))
    with sqlite3.connect(store.database) as db:assert db.execute(f'SELECT count(*) FROM {EVENTS}').fetchone()[0]==0
    assert admin.queue(who,{'space_id':store.space_id})['items'][0]==queued


def test_rescheduled_stale_rejection_preserves_history(tmp_path):
    owned,store,admin,who,request,model,admission,queued=setup(tmp_path)
    result=admin.control_queue(who,command(request,queued,'reschedule',not_before=0))
    ready=admin.generate(who,{**request,'operation_id':'other'})
    admin.apply(who,{'space_id':store.space_id,'operation_id':'publish','patch':ready['patch']})
    assert admin.run_queue(who,{'space_id':store.space_id})['processed']['state']=='rejected'
    assert admin.enqueue(who,admission)['state']=='rejected'
    assert admin.control_queue(who,command(request,queued,'reschedule',not_before=0))==result


def test_legacy_queue_schema_reopens_without_changing_receipt(tmp_path):
    owned,store,admin,who,request,model,admission,queued=setup(tmp_path)
    with sqlite3.connect(store.database) as db:
        db.execute(f'ALTER TABLE {TABLE} DROP COLUMN history_digest')
        db.execute(f'DROP TABLE {EVENTS}')
    restarted=WikiAuthoringAdmin(store,evidence_root=owned['evidence_root'],model=model)
    assert restarted.enqueue(who,admission)==queued
    assert restarted.control_queue(who,command(request,queued))['state']=='cancelled'


def test_control_history_is_bounded_without_breaking_exact_replay(tmp_path,monkeypatch):
    monkeypatch.setattr('knowledge_platform.local.wiki_queue_control.MAX_HISTORY',2)
    owned,store,admin,who,request,model,admission,queued=setup(tmp_path)
    first_body=command(request,queued,'reschedule',not_before=0);first=admin.control_queue(who,first_body)
    second=admin.control_queue(who,command(request,first,'reschedule','two',not_before=0))
    with pytest.raises(ValueError,match='budget'):admin.control_queue(who,command(request,second,'reschedule','three',not_before=0))
    assert admin.control_queue(who,first_body)==first
    assert admin.queue(who,{'space_id':store.space_id})['items'][0]['admission_receipt']==second['result_receipt']
