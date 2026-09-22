import hashlib,json,os,re,subprocess,sys,threading
from pathlib import Path
import pytest
from knowledge_platform.local.writer_authority import assign as _assign,thaw,enroll,suspend,load_binding,journal,lock,read,encoded,digest,main,BINDING
from knowledge_platform.local.workspace import open_persistent_workspace,_open_lock,WorkspaceError
from knowledge_platform.local.wiki_authoring_queue import WikiQueueWorker
from test_knowledge_platform_local_workspace import _catalog

def assign(state,manifest,operation,writer,**kwargs):
 result=_assign(state,manifest,operation,writer,**kwargs)
 if writer=='puddingknowledge':
  head=result['events'][-1]
  pointer={'format':'puddingharness-active-installation/v1','operation_id':head['operation_id'],
   'cutover_manifest_sha256':'a'*64,'prepared_manifest_sha256':head['migration_manifest_sha256'],
   'source_home_identity':'b'*64,'source_freeze_receipt_sha256':'sha256:'+'c'*64,
   'active_installation_revision':head['active_installation_revision'],
   'harness_assigned_event_sha256':'d'*64,'knowledge_assigned_event_sha256':head['sha256'],
   'active_writers':{'session_harness':'puddingharness','knowledge_catalog':'puddingknowledge','connector_jobs':'puddingknowledge'}}
  path=state/'active-installation.json';path.write_bytes(encoded(pointer));path.chmod(0o600)
 return result

@pytest.fixture
def roots(tmp_path):
 root=tmp_path.resolve();catalog=root/'source.sqlite3';_catalog(catalog);wiki=root/'source-wiki';wiki.mkdir();(wiki/'guide.md').write_text('# Guide\n')
 state=root/'state'
 with open_persistent_workspace(state,catalog=catalog,wiki_root=wiki):pass
 return state,root/'authority'

def test_enrollment_and_owned_workspace_lease(roots):
 state,authority=roots;first=enroll(state,authority,'enroll-1');assert enroll(state,authority,'enroll-1')==first
 workspace=open_persistent_workspace(state)
 try:
  assert workspace.authority_fd is not None
  with pytest.raises(BlockingIOError):
   with lock(authority,exclusive=True):pass
  with pytest.raises(WorkspaceError):suspend(state,'suspend-1')
 finally:workspace.__exit__(None,None,None)
 assert len(journal(load_binding(state))['events'])==1

def test_suspension_replay_and_revision_denial(roots):
 state,authority=roots;enroll(state,authority,'enroll-1');result=suspend(state,'suspend-1');assert result==suspend(state,'suspend-1')
 assert result['events'][-1]['writers']=={'knowledge_catalog':None,'connector_jobs':None}
 with pytest.raises(WorkspaceError):open_persistent_workspace(state)
 (state/'.workspace-freeze-v1.json').unlink()
 with pytest.raises(ValueError,match='no active Knowledge'):open_persistent_workspace(state)
 with pytest.raises(Exception):suspend(state,'suspend-1')
 assert not (state/'.workspace-freeze-v1.json').exists()
 reopened=_open_lock(state);os.close(reopened)

@pytest.mark.parametrize('mode',['missing','corrupt','part','manifest','authority_moved'])
def test_invalid_binding_or_journal_denies_startup(roots,mode):
 state,authority=roots;enroll(state,authority,'enroll-1')
 if mode=='missing':(authority/'journal.json').unlink()
 if mode=='corrupt':(authority/'journal.json').write_text('{}\n')
 if mode=='part':(state/(BINDING+'.part')).write_text('partial')
 if mode=='manifest':(state/'workspace.json').write_text('{}')
 if mode=='authority_moved':authority.rename(authority.with_name('old'));authority.mkdir(mode=0o700)
 with pytest.raises((ValueError,OSError)):open_persistent_workspace(state)
 fd=_open_lock(state);os.close(fd)

def test_existing_workspace_cannot_change_authority(roots):
 state,authority=roots;enroll(state,authority,'enroll-1')
 with pytest.raises(ValueError):enroll(state,authority,'other')

def test_suspension_checkpoint_failure_remains_frozen(roots,monkeypatch):
 import knowledge_platform.local.writer_authority as module
 state,authority=roots;enroll(state,authority,'enroll-1');original=module._replace
 def fail(*args):raise OSError('injected checkpoint failure')
 monkeypatch.setattr(module,'_replace',fail)
 with pytest.raises(OSError):suspend(state,'suspend-1')
 assert (state/'.workspace-freeze-v1.json').exists() and len(journal(load_binding(state))['events'])==1
 monkeypatch.setattr(module,'_replace',original);assert suspend(state,'suspend-1')['events'][-1]['state']=='suspended'

def test_replace_reader_relaxes_only_the_read_budget(tmp_path):
 import knowledge_platform.local.writer_authority as module
 target=tmp_path/'manifest.json'
 big={'entries':['x'*64]*2000}
 assert len(module.encoded(big))>module.MAX_BYTES
 module._replace(target,big)
 with pytest.raises(ValueError): module._replace(target,big)  # default 64KiB authority reader refuses
 module._replace(target,big,reader=lambda p: json.loads(p.read_bytes()))
 assert json.loads(target.read_bytes())==big

def test_nonprivate_workspace_rejected_without_chmod(roots):
 state,authority=roots;state.chmod(0o755)
 with pytest.raises(Exception):enroll(state,authority,'enroll-1')
 assert state.stat().st_mode&0o777==0o755 and not authority.exists()

def test_worker_keeps_both_leases_after_bounded_shutdown(tmp_path):
 from test_wiki_authoring_generation import prepare,Model
 entered=threading.Event();release=threading.Event()
 class Blocking(Model):
  def generate(self,*args):
   entered.set();assert release.wait(15);return super().generate(*args)
 owned,store,admin,who,request,model=prepare(tmp_path,Blocking())
 state=tmp_path/'state';authority=tmp_path/'authority';enroll(state,authority,'enroll-1')
 workspace=open_persistent_workspace(state)
 admin.enqueue(who,{'request':request,'not_before':0})
 worker=WikiQueueWorker(admin.queue_service,who,workspace_lock_fd=workspace.lock_fd,authority_lock_fd=workspace.authority_fd)
 worker.start()
 try:
  assert entered.wait(5);worker.close();assert worker.thread.is_alive();workspace.__exit__(None,None,None)
  with pytest.raises(BlockingIOError):
   with lock(authority,exclusive=True):pass
  with pytest.raises(WorkspaceError):suspend(state,'after-worker')
  release.set();worker.thread.join(5);assert not worker.thread.is_alive()
  assert admin.proposal(who,{'space_id':store.space_id,'operation_id':request['operation_id']})['state']=='ready'
  assert suspend(state,'after-worker')['events'][-1]['state']=='suspended'
 finally:release.set();worker.thread.join(5)

def test_worker_thread_start_failure_releases_only_duplicates(roots,monkeypatch):
 state,authority=roots;enroll(state,authority,'enroll-1');workspace=open_persistent_workspace(state)
 class Queue:
  from types import SimpleNamespace
  store=SimpleNamespace(database=state/'catalog.sqlite3')
 worker=WikiQueueWorker(Queue(),None,workspace_lock_fd=workspace.lock_fd,authority_lock_fd=workspace.authority_fd)
 def fail():raise RuntimeError('start failed')
 monkeypatch.setattr(worker.thread,'start',fail)
 try:
  with pytest.raises(RuntimeError):worker.start()
  assert worker._workspace_fd is None and worker._authority_fd is None
  with pytest.raises(BlockingIOError):
   with lock(authority,exclusive=True):pass
  with pytest.raises(WorkspaceError):_open_lock(state)
 finally:workspace.__exit__(None,None,None)
 with lock(authority,exclusive=True):pass

def test_worker_authority_dup_failure_cleans_workspace_duplicate(roots):
 state,authority=roots;enroll(state,authority,'enroll-1');workspace=open_persistent_workspace(state)
 class Queue:
  from types import SimpleNamespace
  store=SimpleNamespace(database=state/'catalog.sqlite3')
 worker=WikiQueueWorker(Queue(),None,workspace_lock_fd=workspace.lock_fd,authority_lock_fd=-1)
 try:
  with pytest.raises(OSError):worker.start()
  assert worker._workspace_fd is None and worker._authority_fd is None
 finally:workspace.__exit__(None,None,None)
 fd=_open_lock(state);os.close(fd)


def test_complete_binding_publication_failure_resumes(roots,monkeypatch):
 import knowledge_platform.local.writer_authority as module
 state,authority=roots;original=module.os.replace
 def fail(source,target):
  if Path(target)==state/BINDING:raise OSError('injected binding publication failure')
  return original(source,target)
 monkeypatch.setattr(module.os,'replace',fail)
 with pytest.raises(OSError):enroll(state,authority,'enroll-1')
 assert (state/(BINDING+'.part')).exists()
 with pytest.raises(ValueError,match='incomplete'):open_persistent_workspace(state)
 monkeypatch.setattr(module.os,'replace',original)
 assert enroll(state,authority,'enroll-1')['events'][0]['state']=='existing_writer'
 with open_persistent_workspace(state):pass


def test_unexpected_workspace_file_rejects_before_enrollment(roots):
 state,authority=roots;(state/'unknown').write_text('unrelated')
 with pytest.raises(WorkspaceError):enroll(state,authority,'enroll-1')
 assert not authority.exists() and not (state/BINDING).exists()


def test_enrolled_worker_refuses_missing_or_wrong_workspace_lease(roots):
 from types import SimpleNamespace
 state,authority=roots;enroll(state,authority,'enroll-1');workspace=open_persistent_workspace(state)
 queue=SimpleNamespace(store=SimpleNamespace(database=state/'catalog.sqlite3'))
 other=state.parent/'other';other.mkdir(mode=0o700);wrong=_open_lock(other)
 try:
  with pytest.raises(ValueError):WikiQueueWorker(queue,None).start()
  with pytest.raises(ValueError):WikiQueueWorker(queue,None,workspace_lock_fd=wrong,authority_lock_fd=workspace.authority_fd).start()
 finally:os.close(wrong);workspace.__exit__(None,None,None)


def test_worker_refuses_hardlinked_catalog(roots):
 from types import SimpleNamespace
 state,authority=roots;enroll(state,authority,'enroll-1');workspace=open_persistent_workspace(state)
 os.link(state/'catalog.sqlite3',state.parent/'shared.sqlite3')
 queue=SimpleNamespace(store=SimpleNamespace(database=state/'catalog.sqlite3'))
 try:
  with pytest.raises(ValueError,match='unlinked'):WikiQueueWorker(queue,None,workspace_lock_fd=workspace.lock_fd,authority_lock_fd=workspace.authority_fd).start()
 finally:workspace.__exit__(None,None,None)


def test_hardlinked_catalog_rejects_enrollment_and_api_admission(roots):
 state,authority=roots;shared=state.parent/'shared.sqlite3';os.link(state/'catalog.sqlite3',shared)
 with pytest.raises(ValueError,match='unlinked'):enroll(state,authority,'enroll-1')
 assert not (state/BINDING).exists()
 shared.unlink();enroll(state,authority,'enroll-1');os.link(state/'catalog.sqlite3',shared)
 with pytest.raises(ValueError,match='unlinked'):open_persistent_workspace(state)


def _manifest(root,state,*,rollback_digest=None,suffix=''):
 mappings=[{'domain':'knowledge_catalog','source_id':'asset-1','resource_uri':'knowledge://assets/asset-1'}]
 summaries=[];coverage=[]
 for index,domain in enumerate(('session_harness','knowledge_catalog','connector_jobs')):
  domain_mappings=[{'source_id':item['source_id'],'resource_uri':item['resource_uri']}
                   for item in mappings if item['domain']==domain]
  count=len(domain_mappings);source_digest='sha256:'+str(index+3)*64
  summaries.append({'domain':domain,'object_count':count,'source_digest':source_digest})
  coverage.append({'domain':domain,'source_count':count,'target_count':count,'mapped_count':count,
   'source_ids_sha256':source_digest,'target_ids_sha256':'sha256:'+str(index+6)*64,
   'mapping_sha256':'sha256:'+hashlib.sha256(json.dumps(domain_mappings,sort_keys=True,separators=(',',':')).encode()).hexdigest(),
   'mapping_coverage_bps':10000,'zero_object_attested':count==0,
   'source_producer_format':'puddingclaw-cutover-domain-inventory/v1',
   'target_producer_format':('puddingharness-cutover-domain-inventory/v1' if domain=='session_harness'
                             else 'puddingknowledge-cutover-domain-inventory/v1'),
   'source_inventory_receipt_sha256':'sha256:'+'a'*64,
   'target_inventory_receipt_sha256':'sha256:'+'b'*64,
   'target_artifact_sha256':'sha256:'+'c'*64,'source_producer':'puddingclaw',
   'target_producer':'puddingharness' if domain=='session_harness' else 'puddingknowledge'})
 value={'format':'agent-knowledge-platform-installation-migration/v1',
  'source':{'installation_id':'inst-1'+suffix,'schema_revision':'rev-1','catalog_revision':'rev-1'},
  'targets':{'puddingknowledge':'puddingknowledge-local@0.1.0'},
  'object_summaries':summaries,'id_resource_mappings':mappings,'mapping_coverage':coverage,
  'credential_rebinds':[],
  'active_writers':{'session_harness':'puddingclaw','knowledge_catalog':'puddingclaw','connector_jobs':'puddingclaw'},
  'checkpoint':{'stage':'prepared','knowledge_readiness_receipt_digest':'sha256:'+'d'*64,
   'source_freeze_receipt_sha256':'sha256:'+'e'*64,'source_freeze_evidence_sha256':'sha256:'+'f'*64,
   'source_admission_capability_sha256':'sha256:'+'1'*64,'source_freeze_operation_id':'op-1',
  'source_home_identity':'2'*64},'rollback_strategy':'no_write_until_finalized','state':state,
  'rollback_window_open':True}
 if state=='DISCOVERED':
  value['object_summaries']=[{'domain':'session_harness','object_count':0,
                              'source_digest':'sha256:'+'3'*64}]
  value['id_resource_mappings']=[];del value['mapping_coverage']
 if state in {'CUTOVER','FINALIZED'}:
  value['active_writers']={'session_harness':'puddingharness','knowledge_catalog':'puddingknowledge',
                           'connector_jobs':'puddingknowledge'}
  value['active_installation_revision']='sha256:'+'8'*64
  value['checkpoint']['harness_assigned_event_sha256']='sha256:'+'6'*64
  value['checkpoint']['knowledge_assigned_event_sha256']='sha256:'+'7'*64
  value['completed_at']=None
 if state=='FINALIZED':
  value['rollback_window_open']=False;value['completed_at']='2026-09-22T00:00:00Z'
 if rollback_digest is not None:value['rollback_evidence_digest']='sha256:'+rollback_digest
 path=root/f'manifest-{state.lower()}{suffix}.json'
 path.write_bytes((json.dumps(value,sort_keys=True,separators=(',',':'))+'\n').encode());path.chmod(0o600)
 return path

def _evidence(root,name='reverse-evidence.json'):
 path=root/name;path.write_bytes(('{"reverse":"candidate","name":'+json.dumps(name)+'}\n').encode());path.chmod(0o600)
 return path,hashlib.sha256(path.read_bytes()).hexdigest()

def test_assign_and_thaw_restores_self_writes(roots):
 state,authority=roots;enroll(state,authority,'op-1');suspend(state,'op-1')
 manifest=_manifest(state.parent,'PREPARED')
 result=assign(state,manifest,'op-1','puddingknowledge')
 assert result==assign(state,manifest,'op-1','puddingknowledge')
 head=result['events'][-1]
 assert head['revision']==2 and head['state']=='assigned'
 assert head['writers']=={'knowledge_catalog':'puddingknowledge','connector_jobs':'puddingknowledge'}
 assert head['freeze_receipt_sha256']==result['events'][1]['freeze_receipt_sha256'] and head['rollback_evidence_sha256'] is None
 with pytest.raises(WorkspaceError):open_persistent_workspace(state)
 report=thaw(state,manifest,'op-1')
 assert report==thaw(state,manifest,'op-1') and report['activation_allowed'] is True
 assert not (state/'.workspace-freeze-v1.json').exists()
 retired=authority/'freeze-marker-rev2.json';receipt=authority/'thaw-receipt-rev2.json'
 assert hashlib.sha256(retired.read_bytes()).hexdigest()==head['freeze_receipt_sha256']
 assert read(receipt)['revision_sha256']==head['sha256'] and report['journal']==journal(load_binding(state))
 workspace=open_persistent_workspace(state)
 try:assert workspace.authority_fd is not None
 finally:workspace.__exit__(None,None,None)
 (state/'active-installation.json').unlink()
 with pytest.raises((ValueError,FileNotFoundError)):open_persistent_workspace(state)

def test_assign_requires_suspended_authority(roots):
 state,authority=roots;enroll(state,authority,'op-1')
 manifest=_manifest(state.parent,'PREPARED')
 with pytest.raises(ValueError,match='not suspended'):assign(state,manifest,'op-1','puddingknowledge')
 with pytest.raises(ValueError,match='not assigned'):thaw(state,manifest,'op-1')
 suspend(state,'op-1')
 with pytest.raises(ValueError,match='not assigned'):thaw(state,manifest,'op-1')
 assert len(journal(load_binding(state))['events'])==2


def test_assign_accepts_only_terminal_complete_credential_evidence(roots):
 state,authority=roots;enroll(state,authority,'op-1');suspend(state,'op-1')
 manifest=_manifest(state.parent,'PREPARED');value=json.loads(manifest.read_text())
 value['credential_rebinds']=[
  {'slot':'missing-provider','source_ref_digest':'sha256:'+'8'*64,
   'target_ref':'credential://knowledge/missing-provider','status':'absent'},
  {'slot':'unused-provider','source_ref_digest':'sha256:'+'9'*64,
   'target_ref':'credential://knowledge/unused-provider','status':'not-applicable'},
 ]
 manifest.write_bytes((json.dumps(value,sort_keys=True,separators=(',',':'))+'\n').encode())
 assert assign(state,manifest,'op-1','puddingknowledge')['events'][-1]['state']=='assigned'

def test_assign_and_thaw_bind_the_suspension_operation(roots):
 state,authority=roots;enroll(state,authority,'op-1');suspend(state,'op-1')
 manifest=_manifest(state.parent,'PREPARED')
 with pytest.raises(ValueError,match='operation'):assign(state,manifest,'other','puddingknowledge')
 assign(state,manifest,'op-1','puddingknowledge')
 with pytest.raises(ValueError,match='operation'):thaw(state,manifest,'other')
 with pytest.raises(ValueError,match='assignment changed'):assign(state,_manifest(state.parent,'PREPARED',suffix='-2'),'op-1','puddingknowledge')
 assert thaw(state,manifest,'op-1')['activation_allowed'] is True

@pytest.mark.parametrize('manifest_state',['DISCOVERED','CUTOVER','ROLLED_BACK','FINALIZED'])
def test_forward_assign_requires_prepared_manifest(roots,manifest_state):
 state,authority=roots;enroll(state,authority,'op-1');suspend(state,'op-1')
 with pytest.raises(ValueError,match='PREPARED'):assign(state,_manifest(state.parent,manifest_state),'op-1','puddingknowledge')
 assert len(journal(load_binding(state))['events'])==2

def test_rollback_assign_requires_rolled_back_manifest_and_matching_evidence(roots):
 state,authority=roots;enroll(state,authority,'op-1');suspend(state,'op-1')
 evidence,commitment=_evidence(state.parent)
 with pytest.raises(ValueError,match='ROLLED_BACK'):assign(state,_manifest(state.parent,'PREPARED'),'op-1','puddingclaw',rollback_evidence=evidence)
 rolled=_manifest(state.parent,'ROLLED_BACK',rollback_digest=commitment)
 with pytest.raises(ValueError,match='requires rollback evidence'):assign(state,rolled,'op-1','puddingclaw')
 other,_=_evidence(state.parent,'other-evidence.json')
 with pytest.raises(ValueError,match='does not match'):assign(state,rolled,'op-1','puddingclaw',rollback_evidence=other)
 with pytest.raises(ValueError,match='does not match'):assign(state,_manifest(state.parent,'ROLLED_BACK'),'op-1','puddingclaw',rollback_evidence=evidence)
 with pytest.raises(ValueError,match='no rollback evidence'):assign(state,_manifest(state.parent,'PREPARED',suffix='-3'),'op-1','puddingknowledge',rollback_evidence=evidence)
 assert len(journal(load_binding(state))['events'])==2

def test_rollback_assignment_denies_self_runtime_and_worker(roots):
 from types import SimpleNamespace
 state,authority=roots;enroll(state,authority,'op-1');suspend(state,'op-1')
 forward=_manifest(state.parent,'PREPARED')
 assign(state,forward,'op-1','puddingknowledge');thaw(state,forward,'op-1')
 with open_persistent_workspace(state):pass
 suspend(state,'op-2')
 queue=SimpleNamespace(store=SimpleNamespace(database=state/'catalog.sqlite3'))
 workspace_fd=_open_lock(state)
 try:
  with lock(authority,exclusive=False) as authority_fd:
   with pytest.raises(ValueError,match='suspended'):
    WikiQueueWorker(queue,None,workspace_lock_fd=workspace_fd,authority_lock_fd=authority_fd).start()
 finally:os.close(workspace_fd)
 evidence,commitment=_evidence(state.parent)
 rolled=_manifest(state.parent,'ROLLED_BACK',rollback_digest=commitment)
 result=assign(state,rolled,'op-2','puddingclaw',rollback_evidence=evidence)
 assert result==assign(state,rolled,'op-2','puddingclaw',rollback_evidence=evidence)
 head=result['events'][-1]
 assert head['revision']==4 and head['rollback_evidence_sha256']==commitment
 assert head['writers']=={'knowledge_catalog':'puddingclaw','connector_jobs':'puddingclaw'}
 with pytest.raises(WorkspaceError):open_persistent_workspace(state)
 with pytest.raises(ValueError,match='not assigned'):thaw(state,rolled,'op-2')
 workspace_fd=_open_lock(state)
 try:
  with lock(authority,exclusive=False) as authority_fd:
   with pytest.raises(ValueError,match='assigned to another product'):
    WikiQueueWorker(queue,None,workspace_lock_fd=workspace_fd,authority_lock_fd=authority_fd).start()
 finally:os.close(workspace_fd)
 (state/'.workspace-freeze-v1.json').unlink()
 with pytest.raises(ValueError,match='assigned to another product'):open_persistent_workspace(state)
 reopened=_open_lock(state);os.close(reopened)

@pytest.mark.parametrize('mode',['repeat_suspend','skip_revision','broken_link','missing_binding','extra_field','mixed_writers','unknown_writer','rollback_without_evidence','forward_with_evidence','receipt_mismatch','bad_revision_pointer','bad_manifest_digest'])
def test_assigned_chain_violations_rejected(roots,mode):
 state,authority=roots;enroll(state,authority,'op-1');suspend(state,'op-1')
 binding=load_binding(state);current=journal(binding);head=current['events'][-1]
 value={'revision':2,'previous':head['sha256'],'operation_id':'op-1','state':'assigned',
  'writers':{'knowledge_catalog':'puddingknowledge','connector_jobs':'puddingknowledge'},
  'freeze_receipt_sha256':head['freeze_receipt_sha256'],'active_installation_revision':'sha256:'+'b'*64,
  'migration_manifest_sha256':'c'*64,'rollback_evidence_sha256':None}
 if mode=='repeat_suspend':value={'revision':2,'previous':head['sha256'],'operation_id':'op-1','state':'suspended','writers':{'knowledge_catalog':None,'connector_jobs':None},'freeze_receipt_sha256':head['freeze_receipt_sha256']}
 if mode=='skip_revision':value['revision']=3
 if mode=='broken_link':value['previous']='f'*64
 if mode=='missing_binding':del value['migration_manifest_sha256']
 if mode=='extra_field':value['unexpected']='x'
 if mode=='mixed_writers':value['writers']={'knowledge_catalog':'puddingknowledge','connector_jobs':'puddingclaw'}
 if mode=='unknown_writer':value['writers']={'knowledge_catalog':'puddingharness','connector_jobs':'puddingharness'}
 if mode=='rollback_without_evidence':value['writers']={'knowledge_catalog':'puddingclaw','connector_jobs':'puddingclaw'}
 if mode=='forward_with_evidence':value['rollback_evidence_sha256']='d'*64
 if mode=='receipt_mismatch':value['freeze_receipt_sha256']='e'*64
 if mode=='bad_revision_pointer':value['active_installation_revision']='b'*64
 if mode=='bad_manifest_digest':value['migration_manifest_sha256']='sha256:'+'c'*64
 crafted=dict(value,sha256=digest(value))
 (authority/'journal.json').write_bytes(encoded(dict(current,events=[*current['events'],crafted])))
 with pytest.raises(ValueError):journal(binding)

def test_thaw_interrupted_after_receipt_resumes_exactly(roots):
 state,authority=roots;enroll(state,authority,'op-1');suspend(state,'op-1')
 manifest=_manifest(state.parent,'PREPARED');assign(state,manifest,'op-1','puddingknowledge')
 def stop():raise RuntimeError('injected interruption after receipt')
 with pytest.raises(RuntimeError):thaw(state,manifest,'op-1',_after_receipt=stop)
 assert (authority/'thaw-receipt-rev2.json').exists() and (state/'.workspace-freeze-v1.json').exists()
 with pytest.raises(WorkspaceError):open_persistent_workspace(state)
 report=thaw(state,manifest,'op-1')
 assert report==thaw(state,manifest,'op-1') and report['activation_allowed'] is True
 assert (authority/'freeze-marker-rev2.json').exists() and not (state/'.workspace-freeze-v1.json').exists()

def test_thaw_receipt_write_failure_preserves_freeze(roots,monkeypatch):
 import knowledge_platform.local.writer_authority as module
 state,authority=roots;enroll(state,authority,'op-1');suspend(state,'op-1')
 manifest=_manifest(state.parent,'PREPARED');assign(state,manifest,'op-1','puddingknowledge')
 original=module._replace
 def fail(*args):raise OSError('injected receipt failure')
 monkeypatch.setattr(module,'_replace',fail)
 with pytest.raises(OSError):thaw(state,manifest,'op-1')
 assert (state/'.workspace-freeze-v1.json').exists() and not (authority/'thaw-receipt-rev2.json').exists()
 monkeypatch.setattr(module,'_replace',original)
 assert thaw(state,manifest,'op-1')['activation_allowed'] is True

@pytest.mark.parametrize('sync_failure',[1,2,3])
def test_thaw_directory_sync_failure_retry_is_exact(roots,monkeypatch,sync_failure):
 import knowledge_platform.local.writer_authority as module
 state,authority=roots;enroll(state,authority,'op-1');suspend(state,'op-1')
 manifest=_manifest(state.parent,'PREPARED');assign(state,manifest,'op-1','puddingknowledge')
 original=module._sync;calls=0
 def sync(root):
  nonlocal calls;calls+=1
  if calls==sync_failure:raise OSError('injected directory fsync failure')
  return original(root)
 monkeypatch.setattr(module,'_sync',sync)
 with pytest.raises(OSError):thaw(state,manifest,'op-1')
 monkeypatch.setattr(module,'_sync',original)
 report=thaw(state,manifest,'op-1')
 assert report==thaw(state,manifest,'op-1') and report['activation_allowed'] is True

def test_thaw_receipt_or_retired_marker_tamper_rejected(roots):
 state,authority=roots;enroll(state,authority,'op-1');suspend(state,'op-1')
 manifest=_manifest(state.parent,'PREPARED');assign(state,manifest,'op-1','puddingknowledge');thaw(state,manifest,'op-1')
 receipt=authority/'thaw-receipt-rev2.json';committed=receipt.read_bytes()
 receipt.write_bytes(b'{}\n')
 with pytest.raises(ValueError,match='thaw receipt changed'):thaw(state,manifest,'op-1')
 receipt.write_bytes(committed)
 retired=authority/'freeze-marker-rev2.json';marker=retired.read_bytes()
 retired.write_bytes(b'{}\n')
 with pytest.raises(ValueError,match='Retired freeze marker changed'):thaw(state,manifest,'op-1')
 retired.write_bytes(marker)
 assert thaw(state,manifest,'op-1')['activation_allowed'] is True

def test_thaw_conflicting_or_missing_evidence_rejected(roots):
 state,authority=roots;enroll(state,authority,'op-1');suspend(state,'op-1')
 manifest=_manifest(state.parent,'PREPARED');assign(state,manifest,'op-1','puddingknowledge');thaw(state,manifest,'op-1')
 retired=authority/'freeze-marker-rev2.json';receipt=authority/'thaw-receipt-rev2.json'
 restored=state/'.workspace-freeze-v1.json';restored.write_bytes(retired.read_bytes());restored.chmod(0o600)
 with pytest.raises(ValueError,match='Conflicting'):thaw(state,manifest,'op-1')
 restored.unlink();receipt.unlink()
 with pytest.raises(ValueError,match='receipt missing'):thaw(state,manifest,'op-1')

def test_thaw_rejects_changed_manifest(roots):
 state,authority=roots;enroll(state,authority,'op-1');suspend(state,'op-1')
 manifest=_manifest(state.parent,'PREPARED');assign(state,manifest,'op-1','puddingknowledge')
 with pytest.raises(ValueError,match='thaw manifest changed'):thaw(state,_manifest(state.parent,'PREPARED',suffix='-2'),'op-1')

@pytest.mark.parametrize('mode',[
 'format','missing_key','extra_key','bad_writer','bad_digest','bad_state','duplicate_domain',
 'missing_coverage','bad_mapping_domain','bad_mapping_digest','pending_credential',
])
def test_assign_rejects_structurally_invalid_manifest(roots,mode):
 state,authority=roots;enroll(state,authority,'op-1');suspend(state,'op-1')
 manifest=_manifest(state.parent,'PREPARED')
 value=json.loads(manifest.read_text())
 if mode=='format':value['format']='other/v2'
 if mode=='missing_key':del value['checkpoint']
 if mode=='extra_key':value['unexpected']=1
 if mode=='bad_writer':value['active_writers']['knowledge_catalog']='nobody'
 if mode=='bad_digest':value['object_summaries'][0]['source_digest']='sha256:zz'
 if mode=='bad_state':value['state']='BOGUS'
 if mode=='duplicate_domain':value['object_summaries'].append({'domain':'knowledge_catalog','object_count':0,'source_digest':'sha256:'+'b'*64})
 if mode=='missing_coverage':del value['mapping_coverage']
 if mode=='bad_mapping_domain':value['id_resource_mappings'][0]['domain']='connector_jobs'
 if mode=='bad_mapping_digest':value['mapping_coverage'][1]['mapping_sha256']='sha256:'+'0'*64
 if mode=='pending_credential':value['credential_rebinds']=[{
  'slot':'provider','source_ref_digest':'sha256:'+'9'*64,
  'target_ref':'credential://knowledge/provider','status':'pending'}]
 manifest.write_bytes((json.dumps(value,sort_keys=True,separators=(',',':'))+'\n').encode())
 with pytest.raises(ValueError,match='Installation manifest'):assign(state,manifest,'op-1','puddingknowledge')
 assert len(journal(load_binding(state))['events'])==2

def test_assign_binds_raw_manifest_bytes_not_canonical_form(roots):
 state,authority=roots;enroll(state,authority,'op-1');suspend(state,'op-1')
 manifest=_manifest(state.parent,'PREPARED')
 raw=(json.dumps(json.loads(manifest.read_text()),indent=1)+'\n').encode()
 manifest.write_bytes(raw)
 result=assign(state,manifest,'op-1','puddingknowledge')
 head=result['events'][-1]
 assert head['migration_manifest_sha256']==hashlib.sha256(raw).hexdigest()
 assert head['active_installation_revision']=='sha256:'+hashlib.sha256(raw).hexdigest()


def _harness_receipt_contract_journal(binding):
 # Field-for-field mirror of the committed cross-product validator in
 # PuddingHarness/backend/harness/knowledge_writer_receipt.py (_journal and the
 # validate_receipt wrapper); constants and field rules are copied, not imported.
 value=journal(binding)
 assert set(value)=={'format','binding_sha256','events'} and value['format']=='puddingknowledge-writer-authority/v1'
 assert value['binding_sha256']==digest(binding)
 base={'revision','previous','operation_id','state','writers','freeze_receipt_sha256','sha256'}
 previous=None
 for number,event in enumerate(value['events']):
  expected=base|({'active_installation_revision','migration_manifest_sha256','rollback_evidence_sha256'} if number>0 and number%2==0 else set())
  assert set(event)==expected and type(event['revision']) is int and event['revision']==number and event['previous']==previous
  assert event['sha256']==digest({key:item for key,item in event.items() if key!='sha256'})
  assert re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,159}',event['operation_id'])
  if number==0:
   assert event['state']=='existing_writer' and event['freeze_receipt_sha256'] is None and event['operation_id']==binding['enrollment_id']
   assert event['writers']=={'knowledge_catalog':'puddingknowledge','connector_jobs':'puddingknowledge'}
  elif number%2:
   assert event['state']=='suspended' and event['writers']=={'knowledge_catalog':None,'connector_jobs':None}
   assert re.fullmatch('[0-9a-f]{64}',event['freeze_receipt_sha256'])
  else:
   writers=event['writers']
   assert event['state']=='assigned' and set(writers)=={'knowledge_catalog','connector_jobs'}
   assert writers['knowledge_catalog']==writers['connector_jobs'] and writers['knowledge_catalog'] in ('puddingclaw','puddingknowledge')
   assert re.fullmatch('[0-9a-f]{64}',event['freeze_receipt_sha256'])
   assert event['freeze_receipt_sha256']==value['events'][number-1]['freeze_receipt_sha256']
   assert re.fullmatch('sha256:[0-9a-f]{64}',event['active_installation_revision'])
   assert re.fullmatch('[0-9a-f]{64}',event['migration_manifest_sha256'])
   if writers['knowledge_catalog']=='puddingclaw':assert re.fullmatch('[0-9a-f]{64}',event['rollback_evidence_sha256'])
   else:assert event['rollback_evidence_sha256'] is None
  previous=event['sha256']
 return value

def test_assigned_journal_matches_harness_receipt_contract(roots,capsys):
 state,authority=roots;enroll(state,authority,'op-1');suspend(state,'op-1')
 forward=_manifest(state.parent,'PREPARED');assign(state,forward,'op-1','puddingknowledge')
 assert len(_harness_receipt_contract_journal(load_binding(state))['events'])==3
 thaw(state,forward,'op-1');suspend(state,'op-2')
 evidence,commitment=_evidence(state.parent)
 assign(state,_manifest(state.parent,'ROLLED_BACK',rollback_digest=commitment),'op-2','puddingclaw',rollback_evidence=evidence)
 produced=_harness_receipt_contract_journal(load_binding(state))
 assert len(produced['events'])==5 and produced['events'][-1]['rollback_evidence_sha256']==commitment
 assert main(['status','--state-dir',str(state)])==0
 receipt=json.loads(capsys.readouterr().out)
 assert set(receipt)=={'format','status','journal','installation_cutover_performed'}
 assert receipt['format']=='puddingknowledge-writer-authority/v1' and receipt['status']=='ok'
 assert receipt['installation_cutover_performed'] is False and receipt['journal']==journal(load_binding(state))

def test_cli_assign_status_thaw_fail_closed_reporting(roots):
 state,authority=roots;backend=Path(__file__).resolve().parents[1]
 env=dict(os.environ,PYTHONPATH=str(backend))
 def run(*args):return subprocess.run([sys.executable,'-m','knowledge_platform.local.writer_authority',*args],env=env,cwd=backend,capture_output=True,text=True)
 assert run('enroll','--state-dir',str(state),'--authority',str(authority),'--operation-id','op-1').returncode==0
 assert run('suspend','--state-dir',str(state),'--operation-id','op-1').returncode==0
 manifest=_manifest(state.parent,'PREPARED')
 bad=run('assign','--state-dir',str(state),'--operation-id','other','--manifest',str(manifest),'--writer','puddingknowledge')
 assert bad.returncode==1 and json.loads(bad.stdout)['status']=='error' and json.loads(bad.stdout)['activation_allowed'] is False
 assert run('assign','--state-dir',str(state),'--operation-id','op-1','--manifest',str(manifest),'--writer','puddingknowledge').returncode==0
 status=run('status','--state-dir',str(state))
 assert status.returncode==0
 reported=json.loads(status.stdout);head=reported['journal']['events'][-1]
 assert head['revision']==2 and head['state']=='assigned' and head['writers']['knowledge_catalog']=='puddingknowledge'
 done=run('thaw','--state-dir',str(state),'--operation-id','op-1','--manifest',str(manifest))
 assert done.returncode==0 and json.loads(done.stdout)['status']=='ok'
 again=run('thaw','--state-dir',str(state),'--operation-id','other','--manifest',str(manifest))
 assert again.returncode==1 and json.loads(again.stdout)['activation_allowed'] is False
