"""Knowledge-owned workspace writer enrollment, suspension, reassignment and audited thaw.

This independent implementation uses only the Knowledge distribution and stdlib.
Reassignment commits an assigned revision bound to a verified installation
migration manifest, and audited thaw retires the freeze marker into the
authority record only for a workspace assigned to this Knowledge. It does not
grant cross-product activation or CUTOVER orchestration.
"""
from __future__ import annotations
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import secrets
from contextlib import contextmanager

BINDING = '.workspace-authority-v1.json'
FORMAT = 'puddingknowledge-writer-authority/v1'
MAX_BYTES = 65536
SELF = 'puddingknowledge'
WRITERS = ('puddingclaw', 'puddingknowledge')


def encoded(value):
    return (json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False)+'\n').encode()


def digest(value):return hashlib.sha256(encoded(value)).hexdigest()


def _unique(items):
    result={}
    for key,value in items:
        if key in result:raise ValueError('Duplicate authority JSON key')
        result[key]=value
    return result


def read(path):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_nlink!=1 or info.st_mode&0o077 or info.st_size>MAX_BYTES:
            raise ValueError('Authority file must be private owned regular and bounded')
        raw=os.read(fd,MAX_BYTES+1)
        if len(raw)>MAX_BYTES:raise ValueError('Authority file exceeds budget')
        current=path.lstat()
        if (current.st_dev,current.st_ino,current.st_size)!=(info.st_dev,info.st_ino,len(raw)):
            raise ValueError('Authority file changed')
        value=json.loads(raw,object_pairs_hook=_unique)
        if not isinstance(value,dict) or encoded(value)!=raw:raise ValueError('Authority JSON must be canonical')
        return value
    finally:os.close(fd)


def _json(raw):
    try:value=json.loads(raw.decode('utf-8'),object_pairs_hook=_unique)
    except (UnicodeError,json.JSONDecodeError,ValueError) as error:raise ValueError('Installation manifest is not JSON') from error
    if not isinstance(value,dict):raise ValueError('Installation manifest must be a JSON object')
    return value


def _read_private(path,limit=1024*1024):
    path=_path(path)
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_nlink!=1 or info.st_mode&0o077 or info.st_size>limit:
            raise ValueError('Installation evidence must be private owned regular and bounded')
        raw=bytearray()
        while len(raw)<=limit:
            chunk=os.read(fd,min(65536,limit+1-len(raw)))
            if not chunk:break
            raw.extend(chunk)
        if len(raw)>limit:raise ValueError('Installation evidence exceeds budget')
        current=path.lstat()
        if (current.st_dev,current.st_ino,current.st_size)!=(info.st_dev,info.st_ino,len(raw)):raise ValueError('Installation evidence changed')
        return bytes(raw)
    finally:os.close(fd)


def identity(root):
    info=root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077:
        raise ValueError('Authority roots must be private owned directories')
    return {'path':str(root),'device':info.st_dev,'inode':info.st_ino}


def _path(value):
    root=Path(value).expanduser()
    if not root.is_absolute() or '..' in root.parts or any(p.is_symlink() for p in (root,*root.parents)):
        raise ValueError('Authority paths must be absolute and unlinked')
    for parent in root.parents:
        info=parent.stat()
        if info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX:
            raise ValueError('Authority ancestor is writable without sticky protection')
    return root


@contextmanager
def lock(root,*,exclusive):
    path=root/'.writer-authority.lock'
    fd=os.open(path,os.O_RDWR|os.O_NOFOLLOW|os.O_NONBLOCK|(os.O_CREAT if exclusive else 0),0o600)
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_nlink!=1 or info.st_mode&0o077:
            raise ValueError('Invalid authority lock')
        fcntl.flock(fd,(fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)|fcntl.LOCK_NB)
        current=path.lstat()
        if (current.st_dev,current.st_ino)!=(info.st_dev,info.st_ino):raise ValueError('Authority lock changed')
        yield fd
    finally:os.close(fd)


def _validate_catalog(home):
    info=(home/'catalog.sqlite3').lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_nlink!=1 or info.st_mode&0o077:
        raise ValueError('Enrolled Catalog must be private, owned and unlinked')


def load_binding(home):
    if (home/(BINDING+'.part')).exists() or (home/(BINDING+'.part')).is_symlink():
        raise ValueError('Authority enrollment is incomplete')
    path=home/BINDING
    if not path.exists() and not path.is_symlink():return None
    value=read(path)
    if set(value)!={'format','workspace','authority','enrollment_id','workspace_manifest_sha256'} or value['format']!=FORMAT or value['workspace']!=identity(home):
        raise ValueError('Authority workspace binding changed')
    _validate_catalog(home)
    from .workspace_freeze import _read_record
    from .workspace import _MAX_MANIFEST_BYTES
    raw,_=_read_record(home/'workspace.json',links=1,limit=_MAX_MANIFEST_BYTES)
    if hashlib.sha256(raw).hexdigest()!=value['workspace_manifest_sha256']:
        raise ValueError('Enrolled workspace manifest changed')
    root=_path(value['authority']['path'])
    if value['authority']!=identity(root):raise ValueError('Authority directory changed')
    return value


def journal(binding):
    value=read(Path(binding['authority']['path'])/'journal.json')
    if set(value)!={'format','binding_sha256','events'} or value['format']!=FORMAT or value['binding_sha256']!=digest(binding):
        raise ValueError('Authority journal binding mismatch')
    events=value['events']
    if not isinstance(events,list) or not events:raise ValueError('Invalid authority history')
    base={'revision','previous','operation_id','state','writers','freeze_receipt_sha256','sha256'}
    previous=None
    for number,event in enumerate(events):
        expected=base|({'active_installation_revision','migration_manifest_sha256','rollback_evidence_sha256'} if number>0 and number%2==0 else set())
        if not isinstance(event,dict) or set(event)!=expected or type(event['revision']) is not int or event['revision']!=number or event['previous']!=previous:
            raise ValueError('Invalid authority revision chain')
        payload={key:item for key,item in event.items() if key!='sha256'}
        if event['sha256']!=digest(payload):raise ValueError('Authority revision digest mismatch')
        _operation(event['operation_id'])
        if number==0:
            if event['state']!='existing_writer' or event['writers']!={'knowledge_catalog':'puddingknowledge','connector_jobs':'puddingknowledge'} or event['freeze_receipt_sha256'] is not None or event['operation_id']!=binding['enrollment_id']:
                raise ValueError('Invalid existing writer enrollment')
        elif number%2:
            if event['state']!='suspended' or event['writers']!={'knowledge_catalog':None,'connector_jobs':None} or not _hex64(event['freeze_receipt_sha256']):
                raise ValueError('Invalid suspended authority')
        else:
            writers=event['writers']
            if event['state']!='assigned' or not isinstance(writers,dict) or set(writers)!={'knowledge_catalog','connector_jobs'} or writers['knowledge_catalog']!=writers['connector_jobs'] or writers['knowledge_catalog'] not in WRITERS:
                raise ValueError('Invalid assigned authority')
            if not _hex64(event['freeze_receipt_sha256']) or event['freeze_receipt_sha256']!=events[number-1]['freeze_receipt_sha256']:
                raise ValueError('Invalid assigned freeze commitment')
            if not isinstance(event['active_installation_revision'],str) or not re.fullmatch(r'sha256:[0-9a-f]{64}',event['active_installation_revision']):
                raise ValueError('Invalid active installation revision commitment')
            if not _hex64(event['migration_manifest_sha256']):raise ValueError('Invalid migration manifest commitment')
            if writers['knowledge_catalog']=='puddingclaw':
                if not _hex64(event['rollback_evidence_sha256']):raise ValueError('Invalid rollback evidence commitment')
            elif event['rollback_evidence_sha256'] is not None:raise ValueError('Invalid rollback evidence commitment')
        previous=event['sha256']
    return value


def _hex64(value):
    return isinstance(value,str) and re.fullmatch('[0-9a-f]{64}',value) is not None


def _operation(value):
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,159}',value):raise ValueError('Invalid authority operation')


def _event(number,previous,operation,state,writer,receipt,**extra):
    value={'revision':number,'previous':previous,'operation_id':operation,'state':state,'writers':writer,'freeze_receipt_sha256':receipt,**extra}
    return dict(value,sha256=digest(value))


def acquire_writer(home):
    binding=load_binding(home)
    if binding is None:return None
    root=Path(binding['authority']['path'])
    with lock(root,exclusive=False) as fd:
        current=journal(binding)['events'][-1]
        if current['state']=='assigned':
            if current['writers']['knowledge_catalog']!=SELF:raise ValueError('Workspace writer authority is assigned to another product')
        elif current['state']!='existing_writer':raise ValueError('Workspace has no active Knowledge writer authority')
        if load_binding(home)!=binding:raise ValueError('Authority binding changed')
        return os.dup(fd)


def _sync(root):
    fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)


def _replace(path,value):
    if path.exists() or path.is_symlink():read(path)
    temporary=path.with_name('.'+path.name+'.tmp-'+secrets.token_hex(8))
    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    try:
        data=encoded(value);offset=0
        while offset<len(data):offset+=os.write(fd,data[offset:])
        os.fsync(fd)
    finally:os.close(fd)
    os.replace(temporary,path);_sync(path.parent)


def enroll(state_dir,authority,enrollment_id):
    from . import workspace
    from .workspace_freeze import _owned_root,_read_record
    _operation(enrollment_id);home=_path(state_dir);root=_path(authority)
    if home==root or home.is_relative_to(root) or root.is_relative_to(home):raise ValueError('Authority and workspace must be disjoint')
    _owned_root(home);identity(home)
    fd=workspace._open_lock(home)
    try:
        workspace._check_not_frozen(home)
        # The enrollment partial is validated below; it can be retried while
        # normal startup rejects it. Domain validation remains read-only.
        if any(entry.name not in workspace._allowed_entries() for entry in home.iterdir()):
            raise workspace.WorkspaceError("Workspace contains unexpected entries")
        workspace._load_manifest(home)
        _validate_catalog(home)
        manifest,_=_read_record(home/'workspace.json',links=1,limit=workspace._MAX_MANIFEST_BYTES)
        if not root.exists():root.mkdir(mode=0o700);_sync(root.parent)
        binding={'format':FORMAT,'workspace':identity(home),'authority':identity(root),'enrollment_id':enrollment_id,
                 'workspace_manifest_sha256':hashlib.sha256(manifest).hexdigest()}
        with lock(root,exclusive=True):
            allowed={'.writer-authority.lock','journal.json'}
            artifact=r'(?:thaw-receipt|freeze-marker)-rev[0-9]+\.json'
            temporary=r'\.(?:journal\.json|(?:thaw-receipt|freeze-marker)-rev[0-9]+\.json)\.tmp-[0-9a-f]{16}'
            for entry in root.iterdir():
                if entry.name not in allowed and not re.fullmatch(artifact,entry.name) and not re.fullmatch(temporary,entry.name):raise ValueError('Unknown authority entry')
            existing=home/BINDING;part=home/(BINDING+'.part')
            for path in (existing,part):
                if path.exists() or path.is_symlink():
                    if read(path)!=binding:raise ValueError('Authority enrollment changed')
            first={'format':FORMAT,'binding_sha256':digest(binding),'events':[_event(0,None,enrollment_id,'existing_writer',{'knowledge_catalog':'puddingknowledge','connector_jobs':'puddingknowledge'},None)]}
            if (root/'journal.json').exists() or (root/'journal.json').is_symlink():
                if journal(binding)!=first:raise ValueError('Authority enrollment already suspended or changed')
            else:_replace(root/'journal.json',first)
            if not existing.exists():
                if not part.exists():
                    record_fd=os.open(part,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
                    try:
                        data=encoded(binding);offset=0
                        while offset<len(data):offset+=os.write(record_fd,data[offset:])
                        os.fsync(record_fd)
                    finally:os.close(record_fd)
                _sync(home)
                os.replace(part,existing);_sync(home)
            elif part.exists():raise ValueError('Unexpected authority publication part')
            _sync(root);_sync(home)
            if _read_record(home/'workspace.json',links=1,limit=workspace._MAX_MANIFEST_BYTES)[0]!=manifest:
                raise ValueError('Workspace manifest changed during enrollment')
            return first
    finally:os.close(fd)


def suspend(state_dir,operation_id):
    from .workspace_freeze import freeze_workspace,FREEZE_NAME,_read_record
    _operation(operation_id);home=_path(state_dir);binding=load_binding(home)
    if binding is None:raise ValueError('Workspace is not enrolled')
    before=journal(binding)
    if before['events'][-1]['state']=='suspended':
        if before['events'][-1]['operation_id']!=operation_id:raise ValueError('Authority suspension operation changed')
        raw,_=_read_record(home/FREEZE_NAME,links=1)
        if hashlib.sha256(raw).hexdigest()!=before['events'][-1]['freeze_receipt_sha256']:
            raise ValueError('Committed freeze receipt changed')
    receipt=freeze_workspace(home,operation_id)
    with lock(Path(binding['authority']['path']),exclusive=True):
        if load_binding(home)!=binding:raise ValueError('Authority binding changed')
        current=journal(binding)
        head=current['events'][-1]
        if head['state']=='suspended':
            event=_event(head['revision'],current['events'][-2]['sha256'],operation_id,'suspended',{'knowledge_catalog':None,'connector_jobs':None},receipt['receipt_sha256'])
            if head!=event:raise ValueError('Authority suspension changed')
        else:
            event=_event(len(current['events']),head['sha256'],operation_id,'suspended',{'knowledge_catalog':None,'connector_jobs':None},receipt['receipt_sha256'])
            current=dict(current,events=[*current['events'],event]);_replace(Path(binding['authority']['path'])/'journal.json',current)
        return current


def assign(state_dir,manifest,operation_id,writer,*,rollback_evidence=None):
    from .workspace_freeze import FREEZE_NAME,PART_NAME,_read_record
    from ..distribution.installation_manifest_validator import validate_manifest
    _operation(operation_id);home=_path(state_dir)
    if writer not in WRITERS:raise ValueError('Invalid assigned writer')
    raw=_read_private(manifest)
    document=_json(raw);validate_manifest(document)
    commitment=hashlib.sha256(raw).hexdigest()
    evidence=None
    if writer==SELF:
        if document['state']!='PREPARED':raise ValueError('Assignment to this Knowledge requires a PREPARED installation manifest')
        if rollback_evidence is not None:raise ValueError('Forward assignment carries no rollback evidence')
    else:
        if document['state']!='ROLLED_BACK':raise ValueError('Rollback assignment requires a ROLLED_BACK installation manifest')
        if rollback_evidence is None:raise ValueError('Rollback assignment requires rollback evidence')
        evidence=hashlib.sha256(_read_private(rollback_evidence)).hexdigest()
        if document.get('rollback_evidence_digest')!='sha256:'+evidence:raise ValueError('Rollback evidence does not match the installation manifest')
    binding=load_binding(home)
    if binding is None:raise ValueError('Workspace is not enrolled')
    with lock(Path(binding['authority']['path']),exclusive=True):
        if load_binding(home)!=binding:raise ValueError('Authority binding changed')
        current=journal(binding)
        head=current['events'][-1]
        writers={'knowledge_catalog':writer,'connector_jobs':writer}
        extra={'active_installation_revision':'sha256:'+commitment,'migration_manifest_sha256':commitment,'rollback_evidence_sha256':evidence}
        if head['state']=='assigned':
            event=_event(head['revision'],current['events'][-2]['sha256'],operation_id,'assigned',writers,head['freeze_receipt_sha256'],**extra)
            if head!=event:raise ValueError('Authority assignment changed')
            return current
        if head['state']!='suspended':raise ValueError('Workspace writer authority is not suspended')
        if head['operation_id']!=operation_id:raise ValueError('Authority assignment operation changed')
        if (home/PART_NAME).exists() or (home/PART_NAME).is_symlink():raise ValueError('Freeze publication is incomplete')
        marker,_=_read_record(home/FREEZE_NAME,links=1)
        if hashlib.sha256(marker).hexdigest()!=head['freeze_receipt_sha256']:raise ValueError('Committed freeze receipt changed')
        event=_event(len(current['events']),head['sha256'],operation_id,'assigned',writers,head['freeze_receipt_sha256'],**extra)
        current=dict(current,events=[*current['events'],event])
        _replace(Path(binding['authority']['path'])/'journal.json',current)
        return current


def thaw(state_dir,manifest,operation_id,*,_after_receipt=None):
    from . import workspace
    from .workspace_freeze import FREEZE_NAME,PART_NAME,_read_record
    from ..distribution.installation_manifest_validator import validate_manifest
    _operation(operation_id);home=_path(state_dir)
    raw=_read_private(manifest)
    document=_json(raw);validate_manifest(document)
    commitment=hashlib.sha256(raw).hexdigest()
    binding=load_binding(home)
    if binding is None:raise ValueError('Workspace is not enrolled')
    root=Path(binding['authority']['path'])
    workspace_fd=workspace._open_lock(home)
    try:
        with lock(root,exclusive=True):
            if load_binding(home)!=binding:raise ValueError('Authority binding changed')
            current=journal(binding);head=current['events'][-1]
            if head['state']!='assigned' or head['writers']['knowledge_catalog']!=SELF:raise ValueError('Workspace writer authority is not assigned to this Knowledge')
            if head['operation_id']!=operation_id:raise ValueError('Authority thaw operation changed')
            if head['migration_manifest_sha256']!=commitment or head['active_installation_revision']!='sha256:'+commitment:
                raise ValueError('Authority thaw manifest changed')
            receipt_path=root/f"thaw-receipt-rev{head['revision']}.json"
            retired_path=root/f"freeze-marker-rev{head['revision']}.json"
            marker_path=home/FREEZE_NAME
            if (home/PART_NAME).exists() or (home/PART_NAME).is_symlink():raise ValueError('Freeze publication is incomplete')
            receipt={'format':FORMAT,'state':'thawed','operation_id':operation_id,'writer':SELF,'revision':head['revision'],
                     'revision_sha256':head['sha256'],'freeze_receipt_sha256':head['freeze_receipt_sha256'],
                     'migration_manifest_sha256':head['migration_manifest_sha256'],'active_installation_revision':head['active_installation_revision']}
            retired=retired_path.exists() or retired_path.is_symlink()
            if retired:
                if marker_path.exists() or marker_path.is_symlink():raise ValueError('Conflicting authority thaw state')
                marker,_=_read_record(retired_path,links=1)
                if hashlib.sha256(marker).hexdigest()!=head['freeze_receipt_sha256']:raise ValueError('Retired freeze marker changed')
            else:
                marker,_=_read_record(marker_path,links=1)
                if hashlib.sha256(marker).hexdigest()!=head['freeze_receipt_sha256']:raise ValueError('Committed freeze receipt changed')
            if receipt_path.exists() or receipt_path.is_symlink():
                if read(receipt_path)!=receipt:raise ValueError('Authority thaw receipt changed')
            else:
                if retired:raise ValueError('Authority thaw receipt missing for retired freeze marker')
                _replace(receipt_path,receipt)
            if _after_receipt:_after_receipt()
            if not retired:
                os.rename(marker_path,retired_path)
                _sync(home);_sync(root)
            return {'state':'thawed','thaw_receipt_sha256':hashlib.sha256(encoded(receipt)).hexdigest(),'journal':current,'activation_allowed':True}
    finally:os.close(workspace_fd)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('action',choices=['enroll','suspend','assign','thaw','status']);parser.add_argument('--state-dir',required=True);parser.add_argument('--authority');parser.add_argument('--operation-id');parser.add_argument('--manifest');parser.add_argument('--writer',choices=WRITERS);parser.add_argument('--rollback-evidence')
    args=parser.parse_args(argv)
    try:
        if args.action=='enroll':result=enroll(args.state_dir,args.authority,args.operation_id)
        elif args.action=='suspend':result=suspend(args.state_dir,args.operation_id)
        elif args.action=='assign':result=assign(args.state_dir,args.manifest,args.operation_id,args.writer,rollback_evidence=args.rollback_evidence)
        elif args.action=='thaw':result=thaw(args.state_dir,args.manifest,args.operation_id)['journal']
        else:
            binding=load_binding(_path(args.state_dir))
            if binding is None:raise ValueError('Workspace is not enrolled')
            result=journal(binding)
    except Exception:
        print(json.dumps({'format':FORMAT,'status':'error','activation_allowed':False}));return 1
    print(json.dumps({'format':FORMAT,'status':'ok','journal':result,'installation_cutover_performed':False},sort_keys=True));return 0




def retain_worker_admission(database, workspace_fd, authority_fd):
    """Retain leases bound to the queue's actual owned Catalog, never another fd."""
    home=_path(Path(database).parent)
    binding=load_binding(home)
    if binding is None and workspace_fd is None and authority_fd is None:return (None,None)
    catalog=_path(database)
    if catalog!=home/'catalog.sqlite3':raise ValueError('Worker Catalog is not owned by a workspace')
    info=catalog.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_nlink!=1 or info.st_mode&0o077:
        raise ValueError('Worker Catalog must be private, owned and unlinked')
    if binding is not None and (workspace_fd is None or authority_fd is None):
        raise ValueError('Enrolled Worker requires both workspace and authority admission')
    if authority_fd is not None and binding is None:raise ValueError('Worker authority has no workspace binding')
    duplicates=[]
    try:
        for source,path,mode in (
            (workspace_fd,home/'.workspace.lock',fcntl.LOCK_EX),
            (authority_fd,None if binding is None else Path(binding['authority']['path'])/'.writer-authority.lock',fcntl.LOCK_SH),
        ):
            if source is None:duplicates.append(None);continue
            info=os.fstat(source);current=path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_nlink!=1 or info.st_mode&0o077 or (info.st_dev,info.st_ino)!=(current.st_dev,current.st_ino):
                raise ValueError('Worker admission descriptor belongs to another root')
            copied=os.dup(source);duplicates.append(copied)
            fcntl.flock(copied,mode|fcntl.LOCK_NB)
        if binding is not None:
            head=journal(binding)['events'][-1]
            if head['state']=='assigned':
                if head['writers']['knowledge_catalog']!=SELF:raise ValueError('Worker writer authority is assigned to another product')
            elif head['state']!='existing_writer':raise ValueError('Worker writer authority is suspended')
            if load_binding(home)!=binding:raise ValueError('Worker binding changed')
        return tuple(duplicates)
    except BaseException:
        for copied in duplicates:
            if copied is not None:os.close(copied)
        raise


if __name__=='__main__':raise SystemExit(main())
