import os
import threading
import pytest
from knowledge_platform.local.workspace import _open_lock, open_persistent_workspace, WorkspaceError
from knowledge_platform.local.wiki_authoring_queue import WikiQueueWorker
from test_wiki_authoring_generation import prepare, Model
from knowledge_platform.local.workspace_freeze import freeze_workspace


def test_bounded_shutdown_keeps_workspace_locked_until_final_settlement(tmp_path):
    entered=threading.Event();release=threading.Event()
    class Blocking(Model):
        def generate(self,*args):
            entered.set();assert release.wait(10)
            return super().generate(*args)
    owned,store,admin,who,request,model=prepare(tmp_path,Blocking())
    admin.enqueue(who,{'request':request,'not_before':0})
    workspace=open_persistent_workspace(tmp_path/'state')
    worker=WikiQueueWorker(admin.queue_service,who,workspace_lock_fd=workspace.lock_fd)
    worker.start()
    try:
        assert entered.wait(5)
        worker.close()
        assert worker.thread.is_alive()
        workspace.__exit__(None,None,None)
        with pytest.raises(WorkspaceError):_open_lock(tmp_path/'state')
        with pytest.raises(WorkspaceError):freeze_workspace(tmp_path/'state','after-worker')
        assert admin.proposal(who,{'space_id':store.space_id,'operation_id':request['operation_id']})['state']=='unsettled'
        release.set();worker.thread.join(5)
        assert not worker.thread.is_alive()
        assert admin.proposal(who,{'space_id':store.space_id,'operation_id':request['operation_id']})['state']=='ready'
        fd=_open_lock(tmp_path/'state');os.close(fd)
        assert freeze_workspace(tmp_path/'state','after-worker')['state']=='workspace_frozen'
        with pytest.raises(WorkspaceError,match='frozen'):open_persistent_workspace(tmp_path/'state')
    finally:
        release.set();worker.thread.join(5)


def test_thread_start_failure_releases_duplicate_not_original(tmp_path,monkeypatch):
    owned,store,admin,who,request,model=prepare(tmp_path)
    workspace=open_persistent_workspace(tmp_path/'state')
    fd=workspace.lock_fd
    worker=WikiQueueWorker(admin.queue_service,who,workspace_lock_fd=fd)
    def failed():raise RuntimeError('thread creation failed')
    monkeypatch.setattr(worker.thread,'start',failed)
    try:
        with pytest.raises(RuntimeError):worker.start()
        assert worker._workspace_fd is None
        with pytest.raises(WorkspaceError):_open_lock(tmp_path/'state')
    finally:workspace.__exit__(None,None,None)
    reopened=_open_lock(tmp_path/'state');os.close(reopened)



@pytest.mark.parametrize('kind',['hardlink','public'])
def test_workspace_lock_rejects_unsafe_inode_without_chmod(tmp_path,kind):
    lock=tmp_path/'.workspace.lock';lock.write_text('');lock.chmod(0o600)
    if kind=='hardlink':os.link(lock,tmp_path/'other')
    else:lock.chmod(0o644)
    mode=lock.stat().st_mode
    with pytest.raises(WorkspaceError):_open_lock(tmp_path)
    assert lock.stat().st_mode==mode
