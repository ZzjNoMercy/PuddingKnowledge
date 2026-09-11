"""Migrate the legacy document slice and verified bodies into inactive output."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from urllib.parse import quote

from sqlalchemy import create_engine, text
from knowledge_platform.catalog.rehearsal_runner import run_core_catalog_rehearsal
from knowledge_platform.distribution.sqlite_reverse_delta import _path as _database_path, _file_digest
from knowledge_platform.distribution.catalog_snapshot import _path, _identity

FORMAT = 'puddingknowledge-document-migration/v1'
TOKEN = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$')
MAX_FILE = 32 * 1024 * 1024
MAX_TOTAL = 256 * 1024 * 1024


def _encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()


def _digest(data): return 'sha256:' + hashlib.sha256(data).hexdigest()


def _read(path):
    path = _path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_FILE:
            raise ValueError('Unsupported document file')
        data = stream.read(MAX_FILE + 1)
    if len(data) > MAX_FILE: raise ValueError('Document exceeds budget')
    return data


def _sync_directory(directory):
    fd = os.open(directory, os.O_RDONLY)
    try: os.fsync(fd)
    finally: os.close(fd)


def _atomic(path, data):
    part = _path(str(path) + '.migration-part')
    if part.exists():
        _identity(part)
        if part.stat().st_mode & 0o077: raise ValueError('Migration part permissions are not private')
    fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data); stream.flush(); os.fsync(stream.fileno())
    os.link(part, path)
    part.unlink()
    _sync_directory(path.parent)


def _publish(output, plan, assets, bodies, catalog, validate_catalog, verify_source, after_publish):
    created = False
    if not output.exists():
        output.mkdir(mode=0o700); created = True; _sync_directory(output.parent)
    if not output.is_dir() or output.stat().st_mode & 0o077: raise ValueError('Output must be private')
    lock = _path(output / '.migration.lock')
    if not created and not lock.exists() and not (output/'manifest.json').exists() and not (output/'checkpoint.json').exists():
        raise ValueError('Unowned incomplete migration output')
    if lock.exists():
        _identity(lock)
        if lock.stat().st_mode & 0o077: raise ValueError('Migration output permissions are not private')
    fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        names = {'catalog.sqlite3', *bodies}
        allowed = {'manifest.json', 'checkpoint.json', '.migration.lock', *names}
        allowed |= {name + '.migration-part' for name in allowed if name != '.migration.lock'}
        # A kill after no-replace link and before unlink leaves exactly this
        # owned pair. Remove only a proven same-inode pair with link count two.
        for name in allowed:
            if not name.endswith('.migration-part'): continue
            part = _path(output/name); final_path = _path(output/name.removesuffix('.migration-part'))
            if part.exists() and final_path.exists():
                left, right = part.stat(), final_path.stat()
                if (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino):
                    if not stat.S_ISREG(left.st_mode) or left.st_nlink != 2 or left.st_mode & 0o077:
                        raise ValueError('Invalid interrupted publication link pair')
                    part.unlink(); _sync_directory(part.parent)
        for path in output.rglob('*'):
            _path(path)
            if path.stat().st_mode & 0o077: raise ValueError('Migration output permissions are not private')
            relative = path.relative_to(output).as_posix()
            if path.is_dir():
                if relative != 'blobs': raise ValueError('Unowned migration directory')
            else:
                _identity(path)
                if relative not in allowed: raise ValueError('Unowned migration output')
        checkpoint = {'format': FORMAT, 'state': 'copying', 'plan': plan,
                      'asset_bindings': assets, 'blob_digests': {name: _digest(body) for name, body in bodies.items()},
                      'activation_allowed': False, 'complete_installation_migration': False}
        marker = output / 'checkpoint.json'
        final = output / 'manifest.json'
        complete = final.exists()
        if marker.exists():
            if _encode(json.loads(_read(marker))) != _encode(checkpoint): raise ValueError('Migration checkpoint mismatch')
        elif not complete:
            # A process killed before the first durable checkpoint may leave only
            # its empty lock and incomplete checkpoint write. No payload is adopted.
            leftovers = {p.name for p in output.iterdir()}
            if not created and not leftovers <= {'.migration.lock', 'checkpoint.json.migration-part'}:
                raise ValueError('Unowned incomplete migration output')
            _atomic(marker, _encode(checkpoint))
        manifest = json.loads(_read(final)) if complete else None
        if complete:
            keys = {'format','state','plan','asset_bindings','files','activation_allowed','complete_installation_migration'}
            if set(manifest) != keys or manifest['format'] != FORMAT or manifest['state'] != 'verified_inactive' or manifest['plan'] != plan or manifest['asset_bindings'] != assets or manifest['activation_allowed'] is not False or manifest['complete_installation_migration'] is not False:
                raise ValueError('Migration checkpoint mismatch')
            if not isinstance(manifest['files'],dict) or set(manifest['files']) != names:
                raise ValueError('Migration files mismatch')
        for name in ['catalog.sqlite3', *sorted(bodies)]:
            path = output / name
            if not path.exists():
                if complete: raise ValueError('Completed migration file is missing')
                path.parent.mkdir(mode=0o700,exist_ok=True)
                _atomic(path, catalog.read_bytes() if name == 'catalog.sqlite3' else bodies[name])
                if after_publish: after_publish(name)
            if name == 'catalog.sqlite3':
                validate_catalog(path)
                actual = 'sha256:' + _file_digest(path)
            else:
                actual = _digest(_read(path))
                if actual != _digest(bodies[name]): raise ValueError('Migration output integrity mismatch')
            if complete and actual != manifest['files'][name]: raise ValueError('Migration output integrity mismatch')
        verify_source()
        validate_catalog(output/'catalog.sqlite3')
        for name, body in bodies.items():
            if _digest(_read(output/name)) != _digest(body): raise ValueError('Migration output changed during publication')
        if not complete:
            manifest = {'format': FORMAT, 'state': 'verified_inactive', 'plan': plan, 'asset_bindings': assets,
                        'files': {name: 'sha256:' + _file_digest(output/name) if name == 'catalog.sqlite3' else _digest(_read(output/name)) for name in names},
                        'activation_allowed': False, 'complete_installation_migration': False}
            _atomic(final, _encode(manifest))
        _sync_directory(output)
        return complete
    finally: os.close(fd)


def prepare_document_migration(source_catalog, source_files_root, bindings, output, *,
                               installation_id='document-migration', source_revision='legacy-1', _after_publish=None):
    source = _database_path(source_catalog)
    source_identity = _identity(source)
    root, output = _path(source_files_root), _path(output)
    if not root.is_dir() or not output.parent.is_dir() or output == root or output.is_relative_to(root) or root.is_relative_to(output) or source.is_relative_to(output):
        raise ValueError('Migration paths overlap or are unavailable')
    if not TOKEN.fullmatch(installation_id) or not TOKEN.fullmatch(source_revision):
        raise ValueError('Invalid migration identity')
    if not isinstance(bindings, dict) or len(bindings) > 5000:
        raise ValueError('Invalid document bindings')
    for key, relative in bindings.items():
        if not isinstance(key, str) or not TOKEN.fullmatch(key) or not isinstance(relative, str) or not relative:
            raise ValueError('Invalid document binding')
        p = Path(relative)
        if p.is_absolute() or '..' in p.parts: raise ValueError('Document binding escaped snapshot')
    if output.exists() and (not output.is_dir() or output.stat().st_mode & 0o077):
        raise ValueError('Output must be private')
    with tempfile.TemporaryDirectory(prefix='.knowledge-document-migration-', dir=output.parent) as temp:
        work = Path(temp); copied_source = work / 'source.sqlite3'
        source_digest = 'sha256:' + _file_digest(source, copy_to=copied_source)
        src_engine = create_engine(f'sqlite:///file:{quote(str(copied_source), safe="/")}?mode=ro&immutable=1&uri=true')
        target_path = work / 'catalog.sqlite3'
        target_engine = create_engine(f'sqlite:///{target_path}')
        try:
            with src_engine.connect() as src:
                rows = src.execute(text('SELECT id, content_sha256, source_path, storage_path FROM knowledge_documents')).mappings().all()
                ids = [str(row['id']) for row in rows]
                if len(ids) != len(set(ids)) or set(ids) != set(bindings):
                    raise ValueError('Every document requires exactly one binding')
                bodies = {}; assets = {}; total = 0; facts = {}
                for row in rows:
                    doc_id = str(row['id']); body = _read(root / bindings[doc_id]); total += len(body)
                    if total > MAX_TOTAL: raise ValueError('Document migration exceeds budget')
                    digest = _digest(body)
                    expected = str(row['content_sha256'] or '')
                    if not expected.startswith('sha256:'): expected = 'sha256:' + expected
                    if digest != expected: raise ValueError('Document content digest mismatch')
                    relative = 'blobs/' + digest.removeprefix('sha256:')
                    bodies[relative] = body
                    asset_id = f"asset_{doc_id}_{hashlib.sha256(source_revision.encode()).hexdigest()[:12]}"
                    assets[asset_id] = relative
                    facts[doc_id] = {'relative_path': bindings[doc_id], 'digest': digest}
                plan = {'format': FORMAT, 'source_catalog_digest': source_digest,
                        'source_revision': source_revision, 'installation_id': installation_id,
                        'document_bindings_digest': _digest(_encode(facts))}
                verified_references = {}
                for row in rows:
                    for field in ('source_path', 'storage_path'):
                        if row[field]: verified_references.setdefault(str(row[field]), set()).add(str(row['id']))
                def references_verified(reference):
                    ids = verified_references.get(reference, ())
                    return bool(ids) and all(_digest(_read(root / facts[doc_id]['relative_path'])) == facts[doc_id]['digest'] for doc_id in ids)
                def validate_catalog(path):
                    _identity(_database_path(path))
                    engine = create_engine(f'sqlite:///file:{quote(str(path), safe="/")}?mode=ro&immutable=1&uri=true')
                    try:
                        with engine.connect() as existing:
                            verified = run_core_catalog_rehearsal(src, existing, installation_id=installation_id, source_revision=source_revision, target_revision='document-import-v1', active_revision=source_revision, file_reference_checker=references_verified)
                            if not all(verified.report.checks.values()): raise ValueError('Existing Catalog does not match source')
                    finally: engine.dispose()
                def verify_source():
                    if _identity(_database_path(source)) != source_identity or 'sha256:' + _file_digest(source) != source_digest: raise ValueError('Source changed during migration')
                    for fact in facts.values():
                        if _digest(_read(root / fact['relative_path'])) != fact['digest']: raise ValueError('Document changed during migration')
                with target_engine.begin() as target:
                    result = run_core_catalog_rehearsal(src, target, installation_id=installation_id,
                        source_revision=source_revision, target_revision='document-import-v1', active_revision=source_revision,
                        file_reference_checker=references_verified)
                    if not all(result.report.checks.values()): raise ValueError('Catalog conversion failed')
                target_engine.dispose()
                verify_source()
                complete = _publish(output, plan, assets, bodies, target_path, validate_catalog, verify_source, _after_publish)
                return {'format': FORMAT, 'state': 'verified_inactive', 'document_count': len(rows), 'idempotent': complete, 'activation_allowed': False, 'complete_installation_migration': False}
        finally:
            src_engine.dispose();target_engine.dispose()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-catalog',type=Path,required=True)
    parser.add_argument('--source-files-root',type=Path,required=True)
    parser.add_argument('--bindings',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--installation-id',default='document-migration')
    parser.add_argument('--source-revision',default='legacy-1')
    args=parser.parse_args()
    try:
        result=prepare_document_migration(args.source_catalog,args.source_files_root,json.loads(_read(args.bindings)),args.output,installation_id=args.installation_id,source_revision=args.source_revision)
    except Exception:
        print(json.dumps({'format':FORMAT,'status':'error','error_code':'document_migration_rejected','activation_allowed':False}));return 1
    print(json.dumps(result,sort_keys=True));return 0


if __name__=='__main__':raise SystemExit(main())
