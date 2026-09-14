import json,os,threading
from pathlib import Path
import pytest
from knowledge_platform.local.writer_authority import enroll,suspend,load_binding,journal,lock,BINDING
from knowledge_platform.local.workspace import open_persistent_workspace,_open_lock,WorkspaceError
from knowledge_platform.local.wiki_authoring_queue import WikiQueueWorker
from test_knowledge_platform_local_workspace import _catalog

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
