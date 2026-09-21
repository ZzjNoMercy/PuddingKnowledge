"""Export a suspended owned Knowledge workspace for reverse migration.

Only private copies are opened by SQLite. This is an export, never an activation,
legacy-schema conversion, or installation rollback receipt.
"""
from __future__ import annotations
import argparse
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import re

from . import writer_authority as authority,workspace
from .workspace_freeze import FREEZE_NAME,PART_NAME,_read_record,_sync_directory
from ..distribution import wiki_archive as files
from ..distribution.catalog_normalization import normalize_catalog

FORMAT='puddingknowledge-frozen-workspace-export/v1'
MAX_METADATA=32*1024*1024


def _read_manifest(path):
    raw,_=files._read(path,limit=MAX_METADATA,private=True)
    value=json.loads(raw,object_pairs_hook=authority._unique)
    if not isinstance(value,dict) or authority.encoded(value)!=raw:
        raise ValueError('Export manifest must be canonical')
    return value


def _mkdir(path):
    if path.exists():files._check(path.lstat(),directory=True,private=True);return
    _mkdir(path.parent);path.mkdir(mode=0o700);_sync_directory(path.parent)


def _copy(source,destination,fact):
    _mkdir(destination.parent)
    part=Path(str(destination)+'.export-part')
    fd=os.open(part,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW|os.O_NONBLOCK,0o600)
    try:
        files._check(os.fstat(fd),private=True)
        _,actual=files._read(source,private=True,destination=fd)
        if actual!=fact:raise ValueError('Export source changed during copy')
        os.fsync(fd)
    finally:os.close(fd)
    os.replace(part,destination);_sync_directory(destination.parent)


def _raw_stage(stage,expected):
    allowed=set(expected['files']);allowed.update(name+'.export-part' for name in expected['files'])
    dirs=set(expected['directories'])
    raw=stage/'raw'
    if not raw.exists():return
    files._check(raw.lstat(),directory=True,private=True)
    for parent,children,names in os.walk(raw,followlinks=False):
        for name in children:
            path=Path(parent)/name;files._check(path.lstat(),directory=True,private=True)
            if path.relative_to(raw).as_posix() not in dirs:raise ValueError('Unknown raw export directory')
        for name in names:
            path=Path(parent)/name;files._check(path.lstat(),private=True)
            if path.relative_to(raw).as_posix() not in allowed:raise ValueError('Unknown raw export file')


def export_frozen_workspace(state_dir,output,operation_id,*,_after_copy=None):
    authority._operation(operation_id)
    state,stage=map(authority._path,(state_dir,output))
    binding=authority.load_binding(state)
    if binding is None:raise ValueError('Workspace must already be enrolled')
    authority_root=Path(binding['authority']['path'])
    roots=(state,stage,authority_root)
    if any(a==b or a.is_relative_to(b) or b.is_relative_to(a) for i,a in enumerate(roots) for b in roots[i+1:]):
        raise ValueError('Export roots must be disjoint')
    with ExitStack() as stack:
        fd=workspace._open_lock(state);stack.callback(os.close,fd)
        stack.enter_context(authority.lock(authority_root,exclusive=False))
        journal=authority.journal(binding)
        def verify_authority():
            if authority.load_binding(state)!=binding or authority.journal(binding)!=journal:
                raise ValueError('Workspace authority changed')
            if len(journal['events'])!=2 or journal['events'][-1]['operation_id']!=operation_id:
                raise ValueError('Exact suspended revision is required')
            if (state/PART_NAME).exists() or (state/PART_NAME).is_symlink():
                raise ValueError('Incomplete workspace freeze')
            raw,_=_read_record(state/FREEZE_NAME,links=1)
            if hashlib.sha256(raw).hexdigest()!=journal['events'][-1]['freeze_receipt_sha256']:
                raise ValueError('Workspace freeze commitment changed')
            marker=json.loads(raw,object_pairs_hook=authority._unique)
            expected={'activation_allowed':False,'directory_identity':{'device':state.stat().st_dev,'inode':state.stat().st_ino},
                      'format':'puddingknowledge-workspace-freeze/v1','operation_id':operation_id,
                      'workspace_manifest_sha256':binding['workspace_manifest_sha256'],
                      'root_path_sha256':hashlib.sha256(str(state).encode()).hexdigest(),'state':'workspace_frozen'}
            if authority.encoded(marker)!=authority.encoded(expected) or raw!=authority.encoded(expected):
                raise ValueError('Workspace freeze binding changed')
        verify_authority()
        allowed=workspace._allowed_entries()|{FREEZE_NAME}
        if any(p.name not in allowed for p in state.iterdir()):raise ValueError('Unknown workspace entry')
        if (state/workspace._INITIALIZING).exists():raise ValueError('Incomplete workspace initialization')
        original=files._inventory(state,private=True)
        if any(any(part.endswith('.export-part') for part in Path(name).parts) for name in [*original['files'],*original['directories']]):
            raise ValueError('Reserved export path')
        databases={'catalog':'catalog.sqlite3'}
        if 'retrieval-traces.sqlite3' in original['files']:databases['retrieval-traces']='retrieval-traces.sqlite3'
        if not stage.exists():stage.mkdir(mode=0o700);_sync_directory(stage.parent)
        stage_identity=authority.identity(stage)
        export_fd=stack.enter_context(authority.lock(stage,exclusive=True))
        export_lock=os.fstat(export_fd)
        for entry in stage.iterdir():
            if entry.name in {'.writer-authority.lock','manifest.json','raw','normalized'}:continue
            if re.fullmatch(r'\.manifest.json\.tmp-[0-9a-f]{16}',entry.name):
                files._check(entry.lstat(),private=True);continue
            raise ValueError('Unknown export entry')
        plan={'format':FORMAT,'operation_id':operation_id,'binding':binding,'journal_sha256':authority.digest(journal),
              'source_inventory':original,'output_identity':stage_identity,'databases':databases}
        manifest={'format':FORMAT,'plan':plan,'plan_sha256':authority.digest(plan),
                  'state':'copying','activation_allowed':False,'rollback_completed':False,
                  'legacy_schema_converted':False,'credential_continuity_verified':False}
        if len(authority.encoded(manifest))>MAX_METADATA:raise ValueError('Export metadata budget exceeded')
        previous=None;complete=False
        path=stage/'manifest.json'
        if path.exists() or path.is_symlink():
            previous=_read_manifest(path);complete=previous.get('state')=='verified_frozen_export'
            expected_keys=set(manifest)|({'normalized_databases','normalized_inventory'} if complete else set())
            if set(previous)!=expected_keys or any(authority.encoded({'v':previous[k]})!=authority.encoded({'v':v}) for k,v in manifest.items() if k!='state') or previous['state'] not in ('copying','verified_frozen_export'):
                raise ValueError('Export plan changed')
        else:
            if any(p.name!='.writer-authority.lock' for p in stage.iterdir()):raise ValueError('Unowned export stage')
            authority._replace(path,manifest,reader=_read_manifest)
        _raw_stage(stage,original)
        if complete:
            if files._inventory(stage/'raw',private=True)!=original or files._inventory(stage/'normalized',private=True)!=previous['normalized_inventory']:
                raise ValueError('Completed export content changed')
        _mkdir(stage/'raw')
        for directory in sorted(original['directories'],key=lambda p:tuple(p.split('/'))):_mkdir(stage/'raw'/directory)
        for name,fact in original['files'].items():
            destination=stage/'raw'/name
            if destination.exists():
                if files._read(destination,private=True)[1]!=fact:raise ValueError('Raw export content changed')
            else:
                if complete:raise ValueError('Completed raw export file missing')
                _copy(state/name,destination,fact)
                if _after_copy:_after_copy(name)
        if files._inventory(stage/'raw',private=True)!=original:raise ValueError('Raw export inventory mismatch')
        _mkdir(stage/'normalized')
        if any(p.name not in databases for p in (stage/'normalized').iterdir()):raise ValueError('Unknown normalized database')
        reports={}
        for key,name in databases.items():
            destination=stage/'normalized'/key;_mkdir(destination)
            _,reports[key]=normalize_catalog(stage/'raw'/name,destination)
        verify_authority()
        current=(stage/'.writer-authority.lock').lstat()
        if authority.identity(stage)!=stage_identity or (current.st_dev,current.st_ino)!=(export_lock.st_dev,export_lock.st_ino):
            raise ValueError('Export control changed')
        if files._inventory(state,private=True)!=original or files._inventory(stage/'raw',private=True)!=original:
            raise ValueError('Workspace changed during export')
        result=dict(manifest,state='verified_frozen_export',normalized_databases=reports,
                    normalized_inventory=files._inventory(stage/'normalized',private=True))
        if complete and previous!=result:raise ValueError('Completed export receipt changed')
        authority._replace(path,result,reader=_read_manifest)
        return {'format':FORMAT,'state':'verified_frozen_export','plan_sha256':manifest['plan_sha256'],
                'normalized_databases':reports,'file_count':len(original['files']),'idempotent':complete,
                'workspace_writers_suspended':True,'activation_allowed':False,'rollback_completed':False,
                'legacy_schema_converted':False,'credential_continuity_verified':False}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('state-dir','output','operation-id'):parser.add_argument('--'+name,required=True)
    args=parser.parse_args(argv)
    try:result=export_frozen_workspace(**vars(args))
    except Exception:
        print(json.dumps({'format':FORMAT,'status':'error','activation_allowed':False,'error_code':'frozen_export_rejected'}));return 1
    print(json.dumps(result,sort_keys=True));return 0


if __name__=='__main__':raise SystemExit(main())
