import sqlite3,time,threading
from concurrent.futures import ThreadPoolExecutor
import pytest
from knowledge_contracts import Principal
from knowledge_platform.local.wiki_authoring_admin import WikiAuthoringAdmin
from knowledge_platform.local.wiki_authoring_queue import TABLE,WikiQueueWorker
from test_wiki_authoring_generation import prepare,Model


def test_enqueue_restart_process_once_then_apply(tmp_path):
    owned,store,admin,who,request,model=prepare(tmp_path);before=store.read()
    body={'request':request,'not_before':0};queued=admin.enqueue(who,body)
    assert queued['state']=='queued' and model.calls==0
    assert admin.enqueue(who,body)==queued
    fresh=Model();restarted=WikiAuthoringAdmin(store,evidence_root=owned['evidence_root'],model=fresh)
    result=restarted.run_queue(who,{'space_id':store.space_id})
    assert result['processed']['state']=='ready' and fresh.calls==1 and store.read()==before
    assert restarted.run_queue(who,{'space_id':store.space_id})['processed'] is None
    proposal=restarted.proposal(who,{'space_id':store.space_id,'operation_id':request['operation_id']})
    restarted.apply(who,{'space_id':store.space_id,'operation_id':'publish','patch':proposal['patch']})
    assert len(store.read()[1])==2


def test_due_time_cannot_be_bypassed_by_direct_generate(tmp_path):
    owned,store,admin,who,request,model=prepare(tmp_path)
    admin.enqueue(who,{'request':request,'not_before':int(time.time())+100})
    assert admin.run_queue(who,{'space_id':store.space_id})['processed'] is None
    with pytest.raises(ValueError,match='admission'):admin.generate(who,request)
    assert model.calls==0


def test_stale_queued_revision_is_terminal_rejected(tmp_path):
    owned,store,admin,who,request,model=prepare(tmp_path)
    admin.enqueue(who,{'request':request,'not_before':0})
    ready=admin.generate(who,{**request,'operation_id':'other'})
    admin.apply(who,{'space_id':store.space_id,'operation_id':'publish','patch':ready['patch']})
    assert admin.run_queue(who,{'space_id':store.space_id})['processed']['state']=='rejected'
    assert admin.run_queue(who,{'space_id':store.space_id})['processed'] is None and model.calls==1


def test_unsettled_never_reexecutes_after_restart(tmp_path):
    class Lost(Model):
        def generate(self,*args):raise SystemExit('lost')
    owned,store,admin,who,request,model=prepare(tmp_path,Lost())
    admin.enqueue(who,{'request':request,'not_before':0})
    with pytest.raises(SystemExit):admin.run_queue(who,{'space_id':store.space_id})
    fresh=Model();restarted=WikiAuthoringAdmin(store,evidence_root=owned['evidence_root'],model=fresh)
    assert restarted.run_queue(who,{'space_id':store.space_id})['processed'] is None and fresh.calls==0
    item=restarted.queue(who,{'space_id':store.space_id})['items'][0]
    assert item['state']=='unsettled' and item['execution_liveness']=='unknown'


def test_concurrent_workers_use_unique_generation_claim(tmp_path):
    entered=threading.Event();release=threading.Event()
    class Blocking(Model):
        def generate(self,*args):entered.set();assert release.wait(5);return super().generate(*args)
    owned,store,admin,who,request,model=prepare(tmp_path,Blocking());admin.enqueue(who,{'request':request,'not_before':0})
    with ThreadPoolExecutor(2) as pool:
        running=pool.submit(admin.run_queue,who,{'space_id':store.space_id});assert entered.wait(5)
        assert admin.run_queue(who,{'space_id':store.space_id})['processed'] is None
        release.set();assert running.result()['processed']['state']=='ready'
    assert model.calls==1


def test_worker_never_executes_another_saved_actor(tmp_path):
    owned,store,admin,who,request,model=prepare(tmp_path);admin.enqueue(who,{'request':request,'not_before':0})
    other=Principal(subject_id='other',scopes=who.scopes)
    assert admin.run_queue(other,{'space_id':store.space_id})['processed'] is None
    assert model.calls==0
    with pytest.raises(PermissionError):admin.queue(Principal(subject_id='x',scopes=('knowledge.admin',)),{'space_id':store.space_id})


def test_queue_identity_and_corruption_rejected(tmp_path):
    owned,store,admin,who,request,model=prepare(tmp_path);admin.enqueue(who,{'request':request,'not_before':0})
    with pytest.raises(ValueError):admin.enqueue(who,{'request':{**request,'instruction':'changed'},'not_before':0})
    with sqlite3.connect(store.database) as db:db.execute(f"UPDATE {TABLE} SET request_json='{{}}'")
    with pytest.raises(ValueError):admin.queue(who,{'space_id':store.space_id})
    with pytest.raises(ValueError):admin.run_queue(who,{'space_id':store.space_id})
    assert model.calls==0


def test_queue_pagination_and_invalid_not_before(tmp_path):
    owned,store,admin,who,request,model=prepare(tmp_path)
    for op in ['a','b','c']:admin.enqueue(who,{'request':{**request,'operation_id':op},'not_before':0})
    first=admin.queue(who,{'space_id':store.space_id,'limit':2});assert [x['operation_id'] for x in first['items']]==['a','b'] and first['next_after']=='b'
    last=admin.queue(who,{'space_id':store.space_id,'after':'b','limit':2});assert [x['operation_id'] for x in last['items']]==['c'] and last['next_after'] is None
    with pytest.raises(ValueError):admin.enqueue(who,{'request':request,'not_before':True})


def test_background_worker_stops_on_corrupt_admission(tmp_path):
    owned,store,admin,who,request,model=prepare(tmp_path);admin.enqueue(who,{'request':request,'not_before':0})
    with sqlite3.connect(store.database) as db:db.execute(f"UPDATE {TABLE} SET receipt_digest='bad'")
    worker=WikiQueueWorker(admin.queue_service,who);worker.start();worker.thread.join(5)
    assert worker.status()=={'enabled':True,'running':False,'error':'queue_worker_unavailable'} and model.calls==0


def test_two_workers_selected_same_admission_before_claim(tmp_path,monkeypatch):
    owned,store,admin,who,request,model=prepare(tmp_path);admin.enqueue(who,{'request':request,'not_before':0})
    barrier=threading.Barrier(2);generate=admin.generate
    def simultaneous(*args):barrier.wait(5);return generate(*args)
    monkeypatch.setattr(admin,'generate',simultaneous)
    with ThreadPoolExecutor(2) as pool:
        results=list(pool.map(lambda _:admin.run_queue(who,{'space_id':store.space_id}),range(2)))
    assert model.calls==1 and all(r['processed']['state'] in ('unsettled','ready') for r in results)


def test_sigkill_queue_worker_after_claim_does_not_retry(tmp_path):
    import multiprocessing,os,signal
    class Killed(Model):
        def generate(self,*args):os.kill(os.getpid(),signal.SIGKILL)
    owned,store,admin,who,request,model=prepare(tmp_path,Killed())
    admin.enqueue(who,{'request':request,'not_before':0})
    process=multiprocessing.get_context('fork').Process(target=lambda:admin.run_queue(who,{'space_id':store.space_id}))
    process.start();process.join(10)
    if process.is_alive():process.kill();process.join();pytest.fail('queue worker hung')
    assert process.exitcode==-signal.SIGKILL
    fresh=Model();restarted=WikiAuthoringAdmin(store,evidence_root=owned['evidence_root'],model=fresh)
    assert restarted.run_queue(who,{'space_id':store.space_id})['processed'] is None
    assert restarted.queue(who,{'space_id':store.space_id})['items'][0]['state']=='unsettled' and fresh.calls==0
