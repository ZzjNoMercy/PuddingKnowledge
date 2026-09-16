"""Create a consistent SQLite Catalog snapshot while holding a real write fence."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import time
from urllib.parse import quote

MAX_BYTES = 256 * 1024 * 1024
FORMAT = 'puddingknowledge-catalog-snapshot/v1'


def _path(value):
    path = Path(value).expanduser().absolute()
    if '..' in path.parts or any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('Unsafe Catalog path')
    return path


def _identity(path):
    value = _path(path).stat()
    if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
        raise ValueError('Catalog must be a regular unlinked file')
    return value.st_dev, value.st_ino


def snapshot_catalog(source, output, *, timeout_seconds=5, _while_fenced=None):
    source, output = _path(source), _path(output)
    if type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 60:
        raise ValueError('Invalid fence timeout')
    identity = _identity(source)
    if source.stat().st_size > MAX_BYTES:
        raise ValueError('Catalog exceeds snapshot budget')
    if source == output or output.exists() or not output.parent.is_dir():
        raise ValueError('Output must be new and distinct')
    for path in (source, output):
        for suffix in ('-wal', '-shm', '-journal'):
            sidecar = _path(str(path) + suffix)
            if sidecar.exists():
                _identity(sidecar)
                if sidecar.stat().st_size > MAX_BYTES or path == output:
                    raise ValueError('Unsupported Catalog sidecar')
    descriptor, temporary = tempfile.mkstemp(prefix='.knowledge-snapshot-', suffix='.sqlite3', dir=output.parent)
    os.close(descriptor)
    temporary = Path(temporary)
    deadline = time.monotonic() + timeout_seconds
    fence = reader = target = None
    try:
        uri = f'file:{quote(str(source), safe="/")}'
        fence = sqlite3.connect(uri + '?mode=rw', uri=True, isolation_level=None, timeout=timeout_seconds)
        fence.execute('PRAGMA trusted_schema=OFF')
        fence.execute('BEGIN IMMEDIATE')
        if _identity(source) != identity: raise ValueError('Catalog identity changed')
        # The separate read connection observes the last committed state, including
        # WAL frames. Never use immutable=1 or raw file copying for a live Catalog.
        reader = sqlite3.connect(uri + '?mode=ro', uri=True, isolation_level=None, timeout=timeout_seconds)
        reader.execute('PRAGMA query_only=ON'); reader.execute('PRAGMA trusted_schema=OFF')
        reader.execute('BEGIN')
        pages = reader.execute('PRAGMA page_count').fetchone()[0]
        page_size = reader.execute('PRAGMA page_size').fetchone()[0]
        if pages * page_size > MAX_BYTES: raise ValueError('Catalog exceeds snapshot budget')
        schema_version = reader.execute('PRAGMA schema_version').fetchone()[0]
        target = sqlite3.connect(temporary)
        def progress(status, remaining, total):
            if time.monotonic() > deadline or total * page_size > MAX_BYTES:
                raise ValueError('Snapshot exceeded time or size budget')
        reader.backup(target, pages=128, progress=progress)
        target.execute('PRAGMA journal_mode=DELETE')
        target.execute('PRAGMA trusted_schema=OFF')
        if target.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
            raise ValueError('Snapshot integrity check failed')
        target.close(); target = None
        if _while_fenced: _while_fenced()
        if not fence.in_transaction or _identity(source) != identity:
            raise ValueError('Catalog write fence lost')
        reader.close(); reader = None
        fence.rollback(); fence.close(); fence = None
        if temporary.stat().st_size > MAX_BYTES: raise ValueError('Snapshot exceeds size budget')
        data = temporary.read_bytes()
        with temporary.open('rb') as stream: os.fsync(stream.fileno())
        # Hard-link publication provides no-replace semantics on the same filesystem.
        os.link(temporary, output)
        temporary.unlink()
        directory = os.open(output.parent, os.O_RDONLY)
        try: os.fsync(directory)
        finally: os.close(directory)
        return {'format': FORMAT, 'status': 'snapshot_created',
                'snapshot_digest': 'sha256:' + hashlib.sha256(data).hexdigest(),
                'snapshot_bytes': len(data), 'sqlite_schema_version': schema_version,
                'catalog_fenced_during_snapshot': True, 'writer_fence_held': False,
                'installation_writer_fence_verified': False, 'catalog_schema_verified': False,
                'activation_allowed': False}
    finally:
        for db in (target, reader, fence):
            if db is not None: db.close()
        for suffix in ('', '-wal', '-shm', '-journal'):
            path = Path(str(temporary) + suffix)
            if path.exists(): path.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--timeout-seconds', type=float, default=5)
    args = parser.parse_args()
    try:
        result = snapshot_catalog(args.source, args.output, timeout_seconds=args.timeout_seconds)
    except Exception:
        print(json.dumps({'format': FORMAT, 'status': 'error', 'error_code': 'catalog_snapshot_rejected', 'activation_allowed': False}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__': raise SystemExit(main())
