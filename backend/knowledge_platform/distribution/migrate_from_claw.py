"""Versioned process boundary for offline Claw migration, owned by Knowledge.

Only document Catalog/body conversion is currently implemented. The receipt
explicitly lists pending domains and never grants installation PREPARED/CUTOVER.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import stat

from .catalog_snapshot import _path, _identity
from .catalog_normalization import normalize_catalog, _digest_file as _normalization_digest
from .document_migration import TOKEN, _digest, _encode, _read, _sync_directory, prepare_document_migration
from .sqlite_reverse_delta import _file_digest

REQUEST_FORMAT = 'puddingknowledge-migrate-from-claw-request/v1'
FORMAT = 'puddingknowledge-migrate-from-claw-receipt/v1'
_MAX_REQUEST = 1024 * 1024


def _private_read(path, limit=_MAX_REQUEST):
    path = _path(path)
    info = _identity(path)
    if path.stat().st_mode & 0o077 or path.stat().st_size > limit:
        raise ValueError('Migration protocol file must be bounded and private')
    data = _read(path)
    if len(data) > limit or _identity(path) != info:
        raise ValueError('Migration protocol input changed')
    return data


def _json(data):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value: raise ValueError('Duplicate protocol key')
            value[key] = item
        return value
    value = json.loads(data, object_pairs_hook=pairs)
    if not isinstance(value, dict): raise ValueError('Protocol object required')
    return value


def _write(path, data):
    part = _path(str(path)+'.part')
    if part.exists(): _private_read(part)
    fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data); stream.flush(); os.fsync(stream.fileno())
    os.link(part, path)
    part.unlink(); _sync_directory(path.parent)


def migrate_from_claw(request_path, output, *, source_snapshot, _after_candidate=None):
    if not Path(request_path).expanduser().is_absolute() or not Path(output).expanduser().is_absolute():
        raise ValueError('Protocol paths must be absolute')
    request_path, output = _path(request_path), _path(output)
    if not Path(source_snapshot).expanduser().is_absolute(): raise ValueError('Snapshot root must be absolute')
    snapshot = _path(source_snapshot)
    if not snapshot.is_dir(): raise ValueError('Snapshot root is unavailable')
    raw = _private_read(request_path)
    request = _json(raw)
    required = {'format','installation_id','source_revision','source_schema_revision','source_catalog','source_files_root','bindings'}
    if set(request) != required or request['format'] != REQUEST_FORMAT:
        raise ValueError('Unsupported migration request')
    for key in ('installation_id','source_revision','source_schema_revision'):
        if not isinstance(request[key], str) or not TOKEN.fullmatch(request[key]):
            raise ValueError('Invalid migration source identity')
    for key in ('source_catalog', 'source_files_root'):
        if not isinstance(request[key], str) or not Path(request[key]).expanduser().is_absolute():
            raise ValueError('Source paths must be absolute')
    catalog, files_root = _path(request['source_catalog']), _path(request['source_files_root'])
    if not catalog.is_relative_to(snapshot) or not files_root.is_relative_to(snapshot):
        raise ValueError('Knowledge sources must belong to the approved snapshot')
    if output == snapshot or output.is_relative_to(snapshot) or snapshot.is_relative_to(output):
        raise ValueError('Snapshot and target must be disjoint')
    if not isinstance(request['bindings'], dict): raise ValueError('Document bindings required')
    for source in (request_path, catalog, files_root):
        if source == output or source.is_relative_to(output) or output.is_relative_to(source):
            raise ValueError('Migration source and output overlap')
    if not output.exists(): output.mkdir(mode=0o700); _sync_directory(output.parent)
    if not output.is_dir() or output.stat().st_mode & 0o077: raise ValueError('Output must be private')
    lock = _path(output/'.migrate.lock')
    if lock.exists(): _private_read(lock)
    fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o077:
            raise ValueError('Invalid migration protocol lock')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        allowed = {'.migrate.lock','plan.json','plan.json.part','receipt.json','receipt.json.part','candidate','normalization'}
        if any(p.name not in allowed for p in output.iterdir()): raise ValueError('Unknown migration protocol output')
        normalization = _path(output/'normalization')
        if normalization.exists():
            if not normalization.is_dir() or normalization.stat().st_mode & 0o077:
                raise ValueError('Invalid Catalog normalization output')
            normalization_allowed = {'.lock','plan.json','plan.json.part','catalog.sqlite3','report.json','report.json.part','.work'}
            if any(p.name not in normalization_allowed for p in normalization.iterdir()):
                raise ValueError('Unknown Catalog normalization output')
        # Recover only our exact interrupted no-replace publication pair.
        for name in ('plan.json', 'receipt.json'):
            final, part = output/name, output/(name+'.part')
            if final.exists() and part.exists():
                left, right = _path(final).stat(), _path(part).stat()
                if (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino):
                    if not stat.S_ISREG(left.st_mode) or left.st_nlink != 2 or left.st_mode & 0o077:
                        raise ValueError('Invalid interrupted protocol publication')
                    part.unlink(); _sync_directory(output)
        plan = {'format':REQUEST_FORMAT, 'request_digest':_digest(raw), 'source_snapshot_identity':_digest(str(snapshot).encode())}
        plan_path = output/'plan.json'
        if plan_path.exists():
            if _private_read(plan_path) != _encode(plan): raise ValueError('Migration request changed')
        elif any(p.name not in {'.migrate.lock','plan.json.part'} for p in output.iterdir()):
            raise ValueError('Unowned protocol output')
        else: _write(plan_path, _encode(plan))
        receipt_path = output/'receipt.json'
        previous = _private_read(receipt_path) if receipt_path.exists() else None
        if previous is not None:
            previous_artifacts = _json(previous).get('artifacts', {})
            if not isinstance(previous_artifacts, dict): raise ValueError('Invalid previous receipt')
            for relative in ('normalization/plan.json','normalization/catalog.sqlite3','normalization/report.json'):
                if relative in previous_artifacts:
                    if _normalization_digest(output/relative)['digest'] != previous_artifacts[relative]:
                        raise ValueError('Completed normalization artifact changed')
        candidate = output/'candidate'
        # A raw Home snapshot may carry committed pages in WAL (or require a
        # rollback journal). Normalize only a private copy before delegating to
        # document migration; the approved snapshot is never opened as SQLite.
        sidecars = tuple(_path(str(catalog) + suffix) for suffix in ('-wal','-shm','-journal') if _path(str(catalog) + suffix).exists())
        if sidecars:
            normalization.mkdir(mode=0o700, exist_ok=True)
            if normalization.stat().st_mode & 0o077: raise ValueError('Catalog normalization output is not private')
            normalized_catalog, normalization_report = normalize_catalog(catalog, normalization)
            prepare_document_migration(normalized_catalog, files_root, request['bindings'], candidate,
                installation_id=request['installation_id'], source_revision=request['source_revision'])
        else:
            if normalization.exists(): raise ValueError('Source Catalog sidecar bundle changed')
            normalization_report = None
            prepare_document_migration(catalog, files_root, request['bindings'], candidate,
                installation_id=request['installation_id'], source_revision=request['source_revision'])
        if _after_candidate: _after_candidate()
        if normalization_report is not None:
            normalize_catalog(catalog, normalization)
        manifest_bytes = _private_read(candidate/'manifest.json')
        manifest = _json(manifest_bytes)
        artifacts = {'candidate/manifest.json': _digest(manifest_bytes)}
        if normalization_report is not None:
            for relative in ('normalization/catalog.sqlite3', 'normalization/plan.json', 'normalization/report.json'):
                path = output/relative
                artifacts[relative] = 'sha256:' + _file_digest(path) if path.name == 'catalog.sqlite3' else _digest(_private_read(path))
        total = 0
        for relative, expected in manifest['files'].items():
            path = _path(candidate/relative)
            _identity(path)
            info = path.stat(); total += info.st_size
            if info.st_mode & 0o077 or info.st_size > 64*1024*1024 or total > 256*1024*1024:
                raise ValueError('Candidate exceeds artifact budget')
            actual = 'sha256:'+_file_digest(path)
            if actual != expected: raise ValueError('Candidate artifact changed')
            artifacts['candidate/'+relative] = actual
        receipt = {'format':FORMAT,'request_digest':plan['request_digest'],'source_snapshot_identity':plan['source_snapshot_identity'],
            'state':'verified_inactive_partial','artifacts':artifacts,
            'covered_domains':['document_catalog','document_blobs'],
            'pending_domains':['other_catalog_domains','wiki','indexes','knowledge_credentials'],
            'activation_allowed':False,'installation_prepared':False,'writer_fence_verified':False,
            'credential_rebind_required':True}
        if _private_read(request_path) != raw or _private_read(plan_path) != _encode(plan):
            raise ValueError('Migration request changed during delegation')
        result = _encode(receipt)
        if previous is not None and previous != result: raise ValueError('Existing receipt disagrees with candidate')
        if previous is None: _write(receipt_path, result)
        return receipt
    finally:
        os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-snapshot', type=Path, required=True)
    parser.add_argument('--request', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try: result = migrate_from_claw(args.request, args.output, source_snapshot=args.source_snapshot)
    except Exception:
        print(json.dumps({'format':FORMAT,'status':'error','error_code':'migration_request_rejected','activation_allowed':False}))
        return 1
    print(json.dumps(result, sort_keys=True)); return 0


if __name__ == '__main__': raise SystemExit(main())
