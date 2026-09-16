"""Verify and publish a bounded offline SQLite reverse-migration candidate.

Inputs are read-only snapshots, not active installations. No writer is switched.
"""
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import tempfile
from urllib.parse import quote

_IDENTIFIER = re.compile(r'^[A-Za-z_][A-Za-z0-9_]{0,127}$')
_MAX_DATABASE_BYTES = 256 * 1024 * 1024
_MAX_ROWS = 100_000


def _path(value, *, output=False):
    path = Path(value).expanduser().absolute()
    if '..' in path.parts or any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('Unsafe SQLite path')
    for suffix in ('-wal', '-shm', '-journal'):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists() or sidecar.is_symlink():
            raise ValueError('Offline snapshots must not have SQLite sidecars')
    if output:
        if path.exists(): raise FileExistsError('Rollback output already exists')
        if not path.parent.is_dir(): raise ValueError('Output parent is unavailable')
    elif not path.is_file() or path.stat().st_size > _MAX_DATABASE_BYTES:
        raise ValueError('SQLite snapshot is unavailable or exceeds size limit')
    return path


def _file_digest(path, copy_to=None):
    digest = hashlib.sha256(); size = 0
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, 'rb') as source:
        with ExitStack() as stack:
            target = stack.enter_context(open(copy_to, 'xb')) if copy_to else None
            if copy_to: os.chmod(copy_to, 0o600)
            while chunk := source.read(1024 * 1024):
                size += len(chunk)
                if size > _MAX_DATABASE_BYTES: raise ValueError('Snapshot exceeds size limit')
                digest.update(chunk)
                if target: target.write(chunk)
    return digest.hexdigest()


def _q(name): return '"' + name.replace('"', '""') + '"'


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False,
        default=lambda value: {'blob':value.hex()} if isinstance(value, bytes) else None)


def _digest(value): return 'sha256:' + hashlib.sha256(_json(value).encode()).hexdigest()


def _connect(path):
    _path(path)
    db = sqlite3.connect(f'file:{quote(str(path),safe="/")}?mode=ro&immutable=1', uri=True, timeout=5)
    db.execute('PRAGMA query_only=ON'); db.execute('PRAGMA trusted_schema=OFF')
    db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 8 * 1024 * 1024)
    db.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 1024 * 1024)
    db.execute('BEGIN')
    return db


def _snapshot(db):
    objects = db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name").fetchall()
    tables = {}; indexes = {}; total_rows = 0; total_bytes = 0
    for kind,name,table,sql in objects:
        if kind == 'index':
            indexes[name] = (table,sql); continue
        if kind != 'table' or re.search(r'\bVIRTUAL\s+TABLE\b|\bAUTOINCREMENT\b',sql or '',re.I):
            raise ValueError('Unsupported executable or virtual SQLite schema')
        info = db.execute(f'PRAGMA table_xinfo({_q(name)})').fetchall()
        if not info or any(row[6] != 0 for row in info): raise ValueError('Generated or hidden columns are unsupported')
        columns = tuple(row[1] for row in info)
        pk = tuple(row[0] for row in sorted(info,key=lambda row:row[5]) if row[5])
        rows = []; cursor = db.execute(f'SELECT * FROM {_q(name)}')
        while True:
            batch = cursor.fetchmany(64)
            if not batch: break
            for row in batch:
                total_rows += 1; total_bytes += len(_json(row).encode())
                if total_rows > _MAX_ROWS or total_bytes > _MAX_DATABASE_BYTES:
                    raise ValueError('SQLite snapshot row budget exceeded')
                rows.append(tuple(row))
        rows.sort(key=_json)
        tables[name] = {'schema':sql, 'columns':columns, 'pk':pk, 'rows':rows}
    return {'tables':tables, 'indexes':indexes}


def _row_map(table):
    if not table['pk']: raise ValueError('Owned table requires a primary key')
    result = {}
    for row in table['rows']:
        key_values = [row[i] for i in table['pk']]
        if any(value is None for value in key_values): raise ValueError('NULL primary key is unsupported')
        key = _json(key_values)
        if key in result: raise ValueError('Duplicate primary key')
        result[key] = row
    return result


def _layout(snapshot):
    return {'tables':{name:{k:v for k,v in table.items() if k!='rows'} for name,table in snapshot['tables'].items()},
        'indexes':snapshot['indexes']}


def build_rollback_candidate(source_snapshot: Path, target_before: Path, target_after: Path,
                             output: Path, tables: tuple[str,...]) -> dict:
    source,before,after = [_path(p) for p in (source_snapshot,target_before,target_after)]
    destination = _path(output,output=True)
    if len({(p.stat().st_dev,p.stat().st_ino) for p in (source,before,after)}) != 3:
        raise ValueError('Input snapshots must be distinct')
    if not isinstance(tables,tuple) or not 1<=len(tables)<=256 or any(not isinstance(n,str) or not _IDENTIFIER.fullmatch(n) for n in tables) or len(set(tables))!=len(tables):
        raise ValueError('Invalid rollback table allowlist')
    stage = destination.parent / ('.'+destination.name+'.staging-'+secrets.token_hex(8))
    try:
        with ExitStack() as stack:
            # Read originals as bytes only. SQLite sees private immutable copies,
            # so even a WAL-mode header cannot create sidecars beside user inputs.
            private = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='.rollback-inputs-',dir=destination.parent)))
            connections = []; input_digests = []
            for i,path in enumerate((source,before,after)):
                copied = private / (str(i)+'.sqlite3')
                input_digests.append(_file_digest(path,copied))
                db = _connect(copied);stack.callback(db.close);connections.append(db)
                if db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                    raise ValueError('Input snapshot integrity check failed')
            src,pre,post = [_snapshot(db) for db in connections]
            if _layout(pre) != _layout(post): raise ValueError('Target schema or index changed')
            counts = {}
            for name in tables:
                if any(name not in s['tables'] for s in (src,pre,post)): raise ValueError('Owned table is missing')
                if src['tables'][name] != pre['tables'][name]: raise ValueError('Source snapshot conflicts with target baseline')
                if {k:v for k,v in src['indexes'].items() if v[0]==name} != {k:v for k,v in pre['indexes'].items() if v[0]==name}:
                    raise ValueError('Owned index schema changed')
                old,new = _row_map(pre['tables'][name]),_row_map(post['tables'][name])
                counts[name] = {'insert':len(new.keys()-old.keys()),'delete':len(old.keys()-new.keys()),
                    'update':sum(_json(old[k])!=_json(new[k]) for k in old.keys() & new.keys())}
            for name in pre['tables']:
                if name not in tables and pre['tables'][name] != post['tables'][name]:
                    raise ValueError('Unmapped target table changed')
            expected = {'tables':dict(src['tables']), 'indexes':src['indexes']}
            for name in tables: expected['tables'][name] = post['tables'][name]
            fd = os.open(stage,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600);os.close(fd)
            candidate = sqlite3.connect(stage)
            try:
                # The same read transaction was used to inspect the source and to copy it.
                connections[0].backup(candidate)
                candidate.execute('PRAGMA trusted_schema=OFF');candidate.execute('PRAGMA foreign_keys=ON')
                candidate.execute('BEGIN IMMEDIATE');candidate.execute('PRAGMA defer_foreign_keys=ON')
                # Replacing owned rows handles unique-value swaps and order-independent edits.
                for name in tables: candidate.execute(f'DELETE FROM {_q(name)}')
                for name in tables:
                    table = post['tables'][name]
                    columns = ','.join(_q(c) for c in table['columns'])
                    placeholders = ','.join('?' for _ in table['columns'])
                    candidate.executemany(f'INSERT INTO {_q(name)} ({columns}) VALUES ({placeholders})',table['rows'])
                if candidate.execute('PRAGMA foreign_key_check').fetchall(): raise ValueError('Candidate foreign key verification failed')
                if candidate.execute('PRAGMA integrity_check').fetchall() != [('ok',)]: raise ValueError('Candidate integrity check failed')
                actual = _snapshot(candidate)
                if actual != expected: raise ValueError('Candidate does not preserve the expected state')
                candidate.commit()
            finally: candidate.close()
            # A hot source/WAL is not a valid immutable installation snapshot.
            for path,expected_digest in zip((source,before,after),input_digests):
                _path(path)
                if _file_digest(path) != expected_digest: raise ValueError('Input snapshot changed during candidate construction')
            _path(destination,output=True)
            with stage.open('rb') as handle: os.fsync(handle.fileno())
            os.link(stage,destination)  # Atomic no-replace publication.
            directory_fd=os.open(destination.parent,os.O_RDONLY)
            try: os.fsync(directory_fd)
            finally: os.close(directory_fd)
            return {'tables':counts,'source_digest':_digest(src),'target_before_digest':_digest(pre),
                'target_after_digest':_digest(post),'output_digest':_digest(actual),'activation_allowed':False}
    finally:
        if stage.exists(): stage.unlink()
        for suffix in ('-journal','-wal','-shm'):
            sidecar=Path(str(stage)+suffix)
            if sidecar.exists(): sidecar.unlink()
