"""Observe RECORD-verified installed owned files; this is not signed attestation."""
from __future__ import annotations
import base64
import csv
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
from importlib.metadata import distribution

FORMAT = 'puddingknowledge-installed-identity/v1'
ROOTS = ('knowledge_platform', 'knowledge_contracts')
MAX_FILE = 16 * 1024 * 1024
MAX_TOTAL = 64 * 1024 * 1024


def _read(path):
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('Installed path is symlinked')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > MAX_FILE:
            raise ValueError('Installed file is not a bounded regular file')
        with os.fdopen(fd, 'rb', closefd=False) as stream: data = stream.read(MAX_FILE + 1)
        after = os.fstat(fd)
        current = path.lstat()
        fields = lambda s: (s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns,s.st_nlink)
        if fields(before) != fields(after) or fields(after) != fields(current) or len(data) != before.st_size:
            raise ValueError('Installed file changed during inspection')
        return data
    finally: os.close(fd)


def inspect_installation(dist, *, module_file=None):
    root = Path(dist.locate_file('')).absolute()
    if root.name not in {'site-packages', 'dist-packages'}:
        raise ValueError('A noneditable installed distribution is required')
    if module_file is not None and Path(module_file).absolute() != root/'knowledge_platform/distribution/installed_identity.py':
        raise ValueError('Identity module is outside the selected installation')
    version = dist.version
    if not isinstance(version,str) or not re.fullmatch(r'[0-9][A-Za-z0-9.!+_-]{0,79}',version):
        raise ValueError('Invalid installed version')
    if dist.metadata['Name'] != 'puddingknowledge-local':
        raise ValueError('Unexpected installed distribution')
    records = [f for f in (dist.files or ()) if str(f).endswith('.dist-info/RECORD')]
    if len(records) != 1: raise ValueError('Installed RECORD must be unique')
    record_path = Path(dist.locate_file(records[0])).absolute()
    if record_path.parent.parent != root: raise ValueError('Installed metadata is outside installation')
    direct = record_path.parent/'direct_url.json'
    if direct.exists() and json.loads(_read(direct)).get('dir_info',{}).get('editable') is not False:
        # Wheel installs often have archive_info and no dir_info.
        document=json.loads(_read(direct))
        if 'dir_info' in document: raise ValueError('Editable installation is unsupported')
    raw = _read(record_path)
    selected = {}; seen = set()
    metadata_names = {record_path.parent.name+'/'+name for name in ('METADATA','WHEEL')}
    for row in csv.reader(io.StringIO(raw.decode('utf-8'))):
        if len(row) != 3 or row[0] in seen: raise ValueError('Invalid or duplicate RECORD entry')
        name, digest, size = row; seen.add(name)
        if len(seen)>20000: raise ValueError('Installed RECORD exceeds entry budget')
        if not (name.split('/')[0] in ROOTS or name in metadata_names): continue
        rel=PurePosixPath(name)
        if rel.is_absolute() or '..' in rel.parts or str(rel)!=name or '\\' in name or name.endswith('.pyc'):
            raise ValueError('Unsafe owned RECORD entry')
        if not re.fullmatch(r'sha256=[A-Za-z0-9_-]{43}',digest) or not re.fullmatch(r'0|[1-9][0-9]{0,9}',size):
            raise ValueError('Owned RECORD entry lacks a bounded hash and size')
        selected[name]=(digest[7:],int(size))
    if not metadata_names.issubset(selected) or len(selected)>10000:
        raise ValueError('Installed metadata coverage is incomplete')
    actual=set()
    for package in ROOTS:
        start=root/package
        if not start.is_dir() or start.is_symlink(): raise ValueError('Owned package is missing or linked')
        for path in start.rglob('*'):
            if path.is_symlink(): raise ValueError('Owned package contains a symlink')
            if '__pycache__' in path.relative_to(start).parts:
                if path.is_file():
                    if path.suffix != '.pyc' or path.stat().st_nlink != 1: raise ValueError('Unexpected cache entry')
                elif not path.is_dir(): raise ValueError('Unexpected cache entry')
                continue
            if path.is_file(): actual.add(path.relative_to(root).as_posix())
            elif not path.is_dir(): raise ValueError('Owned package contains a special file')
    expected=set(selected)-metadata_names
    if not {package+'/__init__.py' for package in ROOTS}.issubset(expected) or actual != expected: raise ValueError('Owned package inventory differs from RECORD')
    inventory={};total=0
    for name,(digest,size) in sorted(selected.items()):
        data=_read(root/name);total+=len(data)
        if total>MAX_TOTAL or len(data)!=size or base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip('=')!=digest:
            raise ValueError('Installed file differs from RECORD')
        inventory[name]={'sha256':hashlib.sha256(data).hexdigest(),'size_bytes':size}
    if _read(record_path)!=raw: raise ValueError('Installed RECORD changed')
    payload={'package':'puddingknowledge-local','version':version,'files':inventory}
    digest=hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    return {'format':FORMAT,'package':'puddingknowledge-local','version':version,'inventory_sha256':'sha256:'+digest,'file_count':len(inventory),'scope':'owned_distribution_files','authenticated':False}


def main():
    try: result=inspect_installation(distribution('puddingknowledge-local'),module_file=__file__)
    except Exception:
        print(json.dumps({'format':FORMAT,'status':'error','error_code':'installed_identity_rejected'}));return 1
    print(json.dumps(result,sort_keys=True));return 0


if __name__=='__main__': raise SystemExit(main())
