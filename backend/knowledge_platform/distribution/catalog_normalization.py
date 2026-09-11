"""Recover copied raw SQLite state without opening the approved source bundle."""
from __future__ import annotations
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import time

from .catalog_snapshot import _identity, _path
from .document_migration import _encode, _sync_directory

MAX_BYTES = 64 * 1024**2
MAX_BUNDLE_BYTES = 256 * 1024**2
MAX_SECONDS = 5.0
FORMAT = 'puddingknowledge-catalog-normalization/v1'
_SUFFIXES = ('', '-wal', '-shm', '-journal')
_ALLOWED = {'.lock','plan.json','plan.json.part','report.json','report.json.part','catalog.sqlite3','.work'}


def _digest_file(path, destination=None):
    path=_path(path);fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK);target=None
    try:
        before=os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink!=1 or before.st_size>MAX_BYTES:
            raise ValueError('Catalog bundle member is not bounded')
        if destination is not None:
            target=os.open(destination,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        digest=hashlib.sha256();total=0
        while data:=os.read(fd,1024**2):
            total+=len(data)
            if total>MAX_BYTES:raise ValueError('Catalog member exceeds budget')
            digest.update(data)
            if target is not None:
                view=memoryview(data)
                while view:view=view[os.write(target,view):]
        if target is not None:os.fsync(target)
        after=os.fstat(fd);current=path.stat()
        identity=lambda s:(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns,s.st_nlink)
        if identity(before)!=identity(after) or identity(after)!=identity(current):raise ValueError('Catalog member changed')
        return {'digest':'sha256:'+digest.hexdigest(),'size':total}
    finally:
        if target is not None:os.close(target)
        os.close(fd)


def _facts(source):
    result={};total=0
    for suffix in _SUFFIXES:
        path=_path(str(source)+suffix)
        if suffix and not path.exists():continue
        fact=_digest_file(path);result[suffix]=fact;total+=fact['size']
    if total>MAX_BUNDLE_BYTES:raise ValueError('Catalog bundle exceeds budget')
    return result


def _private(path, *, directory=False):
    path=_path(path);info=path.stat()
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)) or info.st_mode&0o077:
        raise ValueError('Normalization objects must be private')
    if not directory and info.st_nlink!=1:raise ValueError('Normalization file is linked')
    return info


def _read(path):
    info=_private(path)
    if info.st_size>1024**2:raise ValueError('Normalization commitment too large')
    with path.open('rb') as stream: data=stream.read(1024**2+1)
    if len(data)>1024**2:raise ValueError('Normalization commitment too large')
    return data


def _recover_pair(final,part):
    if final.exists() and part.exists():
        left,right=_path(final).stat(),_path(part).stat()
        if (left.st_dev,left.st_ino)==(right.st_dev,right.st_ino):
            if not stat.S_ISREG(left.st_mode) or left.st_nlink!=2 or left.st_mode&0o077:
                raise ValueError('Invalid normalization publication pair')
            part.unlink();_sync_directory(final.parent)


def _publish(path,data):
    part=_path(str(path)+'.part');_recover_pair(path,part)
    if path.exists():
        if _read(path)!=data:raise ValueError('Normalization commitment changed')
        return
    if part.exists():_read(part);part.unlink()
    fd=os.open(part,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'wb') as stream:stream.write(data);stream.flush();os.fsync(stream.fileno())
    os.link(part,path);part.unlink();_sync_directory(path.parent)


def _cleanup_work(work, normalized):
    if not work.exists():return
    _private(work,directory=True)
    _recover_pair(normalized,work/'target.sqlite3')
    names={*('source.sqlite3'+s for s in _SUFFIXES),*('target.sqlite3'+s for s in _SUFFIXES)}
    for entry in work.iterdir():
        if entry.name not in names:raise ValueError('Unowned normalization work file')
        _private(entry)
    for entry in work.iterdir():entry.unlink()
    work.rmdir()


def _materialize(copied,target):
    started=time.monotonic();source_db=target_db=None
    def over_time():return time.monotonic()-started>MAX_SECONDS
    try:
        source_db=sqlite3.connect(str(copied),isolation_level=None,timeout=1)
        source_db.set_progress_handler(lambda:int(over_time()),1000)
        source_db.execute('PRAGMA trusted_schema=OFF');source_db.execute('PRAGMA query_only=ON')
        source_db.execute('BEGIN')
        page_size=source_db.execute('PRAGMA page_size').fetchone()[0]
        pages=source_db.execute('PRAGMA page_count').fetchone()[0]
        if pages*page_size>MAX_BYTES or over_time():raise ValueError('Catalog materialization exceeds budget')
        fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600);os.close(fd)
        target_db=sqlite3.connect(str(target),isolation_level=None,timeout=1)
        target_db.set_progress_handler(lambda:int(over_time()),1000)
        target_db.execute('PRAGMA trusted_schema=OFF')
        def progress(status,remaining,total):
            if total*page_size>MAX_BYTES or over_time():raise ValueError('Catalog materialization exceeds budget')
        source_db.backup(target_db,pages=128,progress=progress,sleep=.01)
        target_db.execute('PRAGMA journal_mode=DELETE')
        if target_db.execute('PRAGMA quick_check').fetchone()!=('ok',):raise ValueError('Catalog integrity check failed')
        if target.stat().st_size>MAX_BYTES or over_time():raise ValueError('Catalog materialization exceeds budget')
    finally:
        if source_db is not None:source_db.close()
        if target_db is not None:target_db.close()
    if any(_path(str(target)+s).exists() for s in _SUFFIXES[1:]):raise ValueError('Normalized Catalog has sidecars')
    with target.open('rb') as stream:os.fsync(stream.fileno())


def normalize_catalog(source,output_dir,*,_after_phase=None):
    source,output_dir=_path(source),_path(output_dir)
    if output_dir.is_relative_to(source.parent) or source.parent.is_relative_to(output_dir):
        raise ValueError('Normalization must be outside the source directory')
    _private(output_dir,directory=True)
    lock=_path(output_dir/'.lock')
    if lock.exists():_private(lock)
    fd=os.open(lock,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|os.O_NONBLOCK,0o600)
    try:
        _private(lock);fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if any(p.name not in _ALLOWED for p in output_dir.iterdir()):raise ValueError('Unknown normalization output')
        facts=_facts(source)
        plan={'format':FORMAT,'source_identity':'sha256:'+hashlib.sha256(str(source).encode()).hexdigest(),'members':facts}
        plan_path=output_dir/'plan.json'
        if not plan_path.exists() and any(p.name not in {'.lock','plan.json.part'} for p in output_dir.iterdir()):
            raise ValueError('Normalization work has no ownership plan')
        _publish(plan_path,_encode(plan))
        normalized=output_dir/'catalog.sqlite3';report_path=output_dir/'report.json';work=output_dir/'.work'
        _recover_pair(report_path,output_dir/'report.json.part')
        _recover_pair(normalized,work/'target.sqlite3')
        if report_path.exists():
            _private(normalized)
            fact=_digest_file(normalized)
            report={'format':FORMAT,'state':'verified','plan_digest':'sha256:'+hashlib.sha256(_encode(plan)).hexdigest(),'normalized_catalog':fact}
            if _read(report_path)!=_encode(report):raise ValueError('Normalized Catalog commitment changed')
            _cleanup_work(work,normalized)
            if _facts(source)!=facts:raise ValueError('Source Catalog changed')
            return normalized,report
        _cleanup_work(work,normalized)
        if normalized.exists():_private(normalized);normalized.unlink()
        work.mkdir(mode=0o700);_sync_directory(output_dir)
        for suffix,fact in facts.items():
            if _digest_file(_path(str(source)+suffix),work/('source.sqlite3'+suffix))!=fact:
                raise ValueError('Catalog changed during copy')
        if _facts(source)!=facts:raise ValueError('Catalog changed during copy')
        if _after_phase:_after_phase('copied')
        target=work/'target.sqlite3';_materialize(work/'source.sqlite3',target)
        if _after_phase:_after_phase('materialized')
        if _facts(source)!=facts:raise ValueError('Catalog changed during materialization')
        fact=_digest_file(target)
        os.link(target,normalized);target.unlink();_sync_directory(output_dir)
        report={'format':FORMAT,'state':'verified','plan_digest':'sha256:'+hashlib.sha256(_encode(plan)).hexdigest(),'normalized_catalog':fact}
        _publish(report_path,_encode(report))
        _cleanup_work(work,normalized);_sync_directory(output_dir)
        return normalized,report
    finally:os.close(fd)
