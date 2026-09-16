"""Rebind a verified inactive document candidate to its installation path.

document_reverse binds every primary document body to the absolute path of the
directory that materialized it.  Once the operator moves the candidate tree to
its final installation location, this step rewrites exactly those absolute
path bindings inside the candidate Catalog in one SQLite transaction on a
private digest-pinned copy, publishes atomically, and then re-verifies the
rebound candidate end to end.  The document_reverse manifest is never
modified; the rebind receipt is the only new evidence.  No writer is switched
and the rebound candidate stays inactive.
"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import sqlite3
import stat

from . import wiki_archive as files, sqlite_reverse_delta as sql
from ..local import writer_authority as control
from ..local.workspace_freeze import _sync_directory

FORMAT = 'puddingknowledge-installation-path-rebind/v1'
REVERSE_FORMAT = 'puddingknowledge-document-reverse/v5'
REVERSE_STATE = 'verified_inactive_documents'
STATE = 'verified_installation_path_rebound'
FLAGS = ('indexes_rebuilt', 'rollback_completed', 'activation_allowed', 'installation_cutover_performed')
STORAGE = 'knowledge_documents.storage_path'
ASSETS = 'knowledge_documents.doc_metadata.assets[*].path'
ORIGINAL = 'knowledge_documents.doc_metadata.original_path'
IMAGES = 'knowledge_documents.doc_metadata.multimodal.image_assets_dir'
# Rebound fields, derived from the document_reverse transform pipeline:
# - knowledge_documents.storage_path: document_reverse.py transform
#   body_bindings (str(stage/output_relative)) applied by
#   document_reverse_plan.prepare_document_reverse to every reversed document.
# - knowledge_documents.doc_metadata assets[*].path / original_path /
#   multimodal.image_assets_dir: document_reverse.py verified_refs output_path
#   (str(stage/bodies/input_relative)) applied by
#   document_attachment_metadata.rebind_attachment_metadata to legacy source
#   selectors whose value equals a verified current reference.
# knowledge_documents.source_path is the user's original file location and is
# never rewritten; /knowledge/... virtual routes are root-relative and survive
# relocation unchanged.  Every rebound value sits strictly below the plan's
# candidate_knowledge_root, so one old-to-new prefix rewrite covers them all.
FIELDS = (STORAGE, ASSETS, ORIGINAL, IMAGES)
PART = 'catalog.sqlite3.rebind-part'
BACKUP = 'catalog.sqlite3.rebind-backup'
ENTRIES = {'.writer-authority.lock', 'manifest.json', 'bodies', 'catalog.sqlite3'}
_MAX_ROWS = 100_000
_MAX_RECEIPT = 1024 * 1024
_HEX = re.compile(r'[0-9a-f]{64}')
_WORK = re.compile(r'\.reverse-work-[a-z0-9_]{8}')


class InstallationPathRebindError(ValueError):
    """The candidate, its manifest or its bodies refuse the path rebind."""


def _read_canonical(path, limit):
    raw, _ = files._read(path, limit=limit, private=True)
    value = json.loads(raw, object_pairs_hook=control._unique)
    if not isinstance(value, dict) or control.encoded(value) != raw:
        raise InstallationPathRebindError('Protocol evidence must be canonical JSON')
    return value, raw


def _hex64(value):
    return isinstance(value, str) and _HEX.fullmatch(value) is not None


def _absolute(value):
    if not isinstance(value, str) or not value or '\\' in value or '\x00' in value:
        raise InstallationPathRebindError('Candidate path must be a canonical POSIX path')
    if not value.startswith('/') or value.startswith('//') or value == '/':
        raise InstallationPathRebindError('Candidate path must be an absolute non-root path')
    if '..' in value.split('/') or posixpath.normpath(value) != value:
        raise InstallationPathRebindError('Candidate path must be canonical')
    return value


def _inventory_facts(plan):
    inventory = plan.get('output_inventory')
    if not isinstance(inventory, dict) or set(inventory) != {'files', 'directories'}:
        raise InstallationPathRebindError('Reverse plan has no committed body inventory')
    committed, directories = inventory['files'], inventory['directories']
    if not isinstance(committed, dict) or len(committed) > files.MAX_FILES:
        raise InstallationPathRebindError('Reverse plan body inventory is invalid')
    total = 0
    for name, fact in committed.items():
        files._relative(name)
        if (not isinstance(fact, dict) or set(fact) != {'sha256', 'size_bytes'}
                or not _hex64(fact['sha256']) or type(fact['size_bytes']) is not int or fact['size_bytes'] < 0):
            raise InstallationPathRebindError('Reverse plan body fact is invalid')
        total += fact['size_bytes']
        if total > files.MAX_TOTAL:
            raise InstallationPathRebindError('Reverse plan body budget exceeded')
    if not isinstance(directories, list) or directories != sorted(set(directories)):
        raise InstallationPathRebindError('Reverse plan directory inventory is invalid')
    for name in directories:
        files._relative(name)
    return committed, set(directories)


def _load_manifest(path):
    manifest, raw = _read_canonical(path, files.MAX_JSON)
    keys = {'format', 'plan', 'state', 'activation_allowed', 'rollback_completed',
            'credential_continuity_verified', 'indexes_rebuilt', 'installation_path_rebound',
            'core_receipt', 'identity_map', 'catalog_sha256',
            'derived_metadata_invalidation', 'attachment_rebinding', 'document_routes'}
    if (set(manifest) != keys or manifest['format'] != REVERSE_FORMAT or manifest['state'] != REVERSE_STATE):
        raise InstallationPathRebindError('Not a verified inactive document candidate receipt')
    for flag in ('activation_allowed', 'rollback_completed', 'credential_continuity_verified',
                 'indexes_rebuilt', 'installation_path_rebound'):
        if manifest[flag] is not False:
            raise InstallationPathRebindError('Candidate receipt is not inert')
    if not _hex64(manifest['catalog_sha256']):
        raise InstallationPathRebindError('Candidate Catalog commitment is invalid')
    plan = manifest['plan']
    if not isinstance(plan, dict) or plan.get('format') != REVERSE_FORMAT:
        raise InstallationPathRebindError('Candidate reverse plan is invalid')
    identity = plan.get('output_identity')
    if (not isinstance(identity, dict) or not isinstance(identity.get('path'), str)
            or type(identity.get('device')) is not int or type(identity.get('inode')) is not int):
        raise InstallationPathRebindError('Candidate output identity is invalid')
    root = plan.get('candidate_knowledge_root')
    if _absolute(root) != identity['path'] + '/bodies':
        raise InstallationPathRebindError('Candidate knowledge root does not match its output identity')
    committed, directories = _inventory_facts(plan)
    bodies = plan.get('bodies')
    if not isinstance(bodies, dict) or len(bodies) > 5000:
        raise InstallationPathRebindError('Candidate body bindings are invalid')
    for asset, fact in bodies.items():
        if (not isinstance(asset, str) or not isinstance(fact, dict)
                or set(fact) != {'input_relative', 'output_relative', 'sha256', 'size_bytes'}):
            raise InstallationPathRebindError('Candidate body binding is invalid')
        files._relative(fact['input_relative'])
        output = fact['output_relative']
        if not isinstance(output, str) or not output.startswith('bodies/'):
            raise InstallationPathRebindError('Candidate body output path is invalid')
        if committed.get(output.removeprefix('bodies/')) != {'sha256': fact['sha256'], 'size_bytes': fact['size_bytes']}:
            raise InstallationPathRebindError('Candidate body binding disagrees with its inventory')
    identity_map = manifest['identity_map']
    if not isinstance(identity_map, dict) or set(identity_map) != set(bodies):
        raise InstallationPathRebindError('Candidate identity map disagrees with its body bindings')
    return manifest, raw, root, committed, directories, len(bodies)


def _selectors(metadata):
    # Strict containers, mirroring document_attachment_metadata._selectors.
    assets = metadata.get('assets')
    if assets is not None:
        if not isinstance(assets, list):
            raise InstallationPathRebindError('Candidate metadata assets must be a list')
        for item in assets:
            if not isinstance(item, dict):
                raise InstallationPathRebindError('Candidate metadata assets entries must be objects')
            value = item.get('path')
            if value not in (None, ''):
                yield ASSETS, value
    value = metadata.get('original_path')
    if value not in (None, ''):
        yield ORIGINAL, value
    multimodal = metadata.get('multimodal')
    if multimodal is not None:
        if not isinstance(multimodal, dict):
            raise InstallationPathRebindError('Candidate metadata multimodal must be an object')
        value = multimodal.get('image_assets_dir')
        if value not in (None, ''):
            yield IMAGES, value


def _rebind_value(value, *, old, new, field, committed, directories):
    """Map one candidate-rooted path old->new; None retains a legacy value."""
    if not isinstance(value, str):
        raise InstallationPathRebindError('Candidate path field is not a string')
    if value.startswith(old + '/'):
        _absolute(value)
        suffix = value[len(old) + 1:]
        files._relative(suffix)
        if suffix not in (directories if field == IMAGES else committed):
            raise InstallationPathRebindError('Candidate path escapes its committed body inventory')
        return new + '/' + suffix
    if value.startswith(new + '/'):
        raise InstallationPathRebindError('Candidate path fields diverge')
    return None


def _set_selector(metadata, field, value, target):
    if field == ASSETS:
        for item in metadata['assets']:
            if item.get('path') == value:
                item['path'] = target
    elif field == ORIGINAL:
        metadata['original_path'] = target
    else:
        metadata['multimodal']['image_assets_dir'] = target


def _rebind_metadata(raw, *, old, new, committed, directories, counts):
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise InstallationPathRebindError('Candidate document metadata is invalid')
    try:
        metadata = json.loads(raw, object_pairs_hook=control._unique)
    except ValueError as error:
        raise InstallationPathRebindError('Candidate document metadata is not unique-key JSON') from error
    if not isinstance(metadata, dict):
        raise InstallationPathRebindError('Candidate document metadata must be an object')
    plan = []
    for field, value in _selectors(metadata):
        target = _rebind_value(value, old=old, new=new, field=field, committed=committed, directories=directories)
        if target is not None and target != value:
            plan.append((field, value, target))
            counts[field] += 1
    if not plan:
        return raw
    # Literal-for-literal replacement inside the raw cell: each selected value
    # must occur exactly as often as it is selected, so uncommitted copies of a
    # rebound path (or pre-existing copies of a rebound result) refuse rather
    # than being rewritten silently.
    rewritten = raw
    for value in sorted({item[1] for item in plan}):
        targets = {item[2] for item in plan if item[1] == value}
        if len(targets) != 1:
            raise InstallationPathRebindError('Candidate metadata rewrite is ambiguous')
        multiplicity = sum(item[1] == value for item in plan)
        ascii_literal = json.dumps(value)
        plain_literal = json.dumps(value, ensure_ascii=False)
        present = {literal: rewritten.count(literal) for literal in {ascii_literal, plain_literal}
                   if rewritten.count(literal)}
        if len(present) != 1 or next(iter(present.values())) != multiplicity:
            raise InstallationPathRebindError('Candidate metadata carries uncommitted path copies')
        literal = next(iter(present))
        replacement = json.dumps(targets.pop(), ensure_ascii=literal == ascii_literal)
        if rewritten.count(replacement):
            raise InstallationPathRebindError('Candidate metadata already carries the rebound path')
        rewritten = rewritten.replace(literal, replacement)
    expected = copy.deepcopy(metadata)
    for field, value, target in plan:
        _set_selector(expected, field, value, target)
    try:
        actual = json.loads(rewritten, object_pairs_hook=control._unique)
    except ValueError as error:
        raise InstallationPathRebindError('Candidate metadata rewrite is not valid JSON') from error
    if actual != expected:
        raise InstallationPathRebindError('Candidate metadata rewrite is not exact')
    return rewritten


def _scan(db, *, old, new, committed, directories):
    rows = db.execute('SELECT id, storage_path, doc_metadata FROM knowledge_documents ORDER BY rowid').fetchall()
    if len(rows) > _MAX_ROWS:
        raise InstallationPathRebindError('Candidate document budget exceeded')
    counts = {field: 0 for field in FIELDS}
    expected = {}
    for row_id, storage, metadata_raw in rows:
        target = _rebind_value(storage, old=old, new=new, field=STORAGE, committed=committed, directories=directories)
        if target is None:
            raise InstallationPathRebindError('Candidate storage path escaped its declared root')
        counts[STORAGE] += target != storage
        expected[row_id] = (target, _rebind_metadata(metadata_raw, old=old, new=new,
                                                     committed=committed, directories=directories, counts=counts))
    return rows, expected, counts


def _discard_part(part):
    for suffix in ('', '-wal', '-shm', '-journal'):
        path = Path(str(part) + suffix)
        if path.exists() or path.is_symlink():
            files._check(path.lstat(), private=True)
            path.unlink()


def _rewrite_copy(source, part, *, old, new, committed, directories, pin):
    """Rewrite old->new on a private digest-pinned copy in one transaction."""
    descriptor = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        files._check(os.fstat(descriptor), private=True)
        os.ftruncate(descriptor, 0)
        _, actual = files._read(source, private=True, destination=descriptor)
        if actual['sha256'] != pin:
            raise InstallationPathRebindError('Candidate Catalog changed during copy')
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    db = sqlite3.connect(str(part))
    try:
        db.execute('PRAGMA trusted_schema=OFF')
        db.execute('PRAGMA foreign_keys=OFF')
        db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 8 * 1024 * 1024)
        rows, expected, counts = _scan(db, old=old, new=new, committed=committed, directories=directories)
        original = {row[0]: (row[1], row[2]) for row in rows}
        db.execute('BEGIN IMMEDIATE')
        for row_id, rebound in expected.items():
            if rebound != original[row_id]:
                db.execute('UPDATE knowledge_documents SET storage_path=?, doc_metadata=? WHERE id=?',
                           (*rebound, row_id))
        if db.execute('PRAGMA foreign_key_check').fetchall():
            raise InstallationPathRebindError('Candidate rewrite would orphan a legacy reference')
        if db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise InstallationPathRebindError('Candidate rewrite integrity failed')
        db.commit()
    finally:
        db.close()
    for suffix in ('-wal', '-shm', '-journal'):
        if Path(str(part) + suffix).exists():
            raise InstallationPathRebindError('Candidate rewrite left SQLite sidecars')
    verify = sql._connect(part)
    try:
        again = verify.execute('SELECT id, storage_path, doc_metadata FROM knowledge_documents ORDER BY rowid').fetchall()
    finally:
        verify.close()
    if len(again) != len(rows) or any(row[0] not in expected or expected[row[0]] != (row[1], row[2]) for row in again):
        raise InstallationPathRebindError('Candidate rewrite verification failed')
    with part.open('rb') as handle:
        os.fsync(handle.fileno())
    return counts, len(rows), sql._file_digest(part)


def _verify_bodies(bodies, inventory):
    if files._inventory(bodies, private=True) != inventory:
        raise InstallationPathRebindError('Candidate bodies changed')


def _publish_receipt(path, data):
    part = path.parent / (path.name + '.part')
    if part.exists() or part.is_symlink():
        files._read(part, limit=_MAX_RECEIPT, private=True)
        part.unlink()
        _sync_directory(path.parent)
    if path.exists():
        existing, _ = files._read(path, limit=_MAX_RECEIPT, private=True)
        if existing != data:
            raise InstallationPathRebindError('Existing rebind receipt disagrees')
        return True
    descriptor = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.link(part, path)
    part.unlink()
    _sync_directory(path.parent)
    return False


def rebind_installation_paths(candidate, manifest, output, *, _after_publish=None):
    """Rebind the candidate's committed absolute paths to its current location.

    ``candidate`` is the verified_inactive_documents directory already placed
    at its final installation location, ``manifest`` its document_reverse
    receipt and ``output`` the rebind receipt to publish.  The manifest is
    read-only; an exact retry verifies idempotently and rewrites nothing.
    """
    candidate = files._path(candidate)
    stage_identity = control.identity(candidate)
    manifest_path = files._path(manifest)
    receipt_path = files._path(output)
    if not receipt_path.parent.is_dir() or receipt_path == manifest_path:
        raise InstallationPathRebindError('Invalid rebind receipt path')
    if receipt_path.is_relative_to(candidate):
        raise InstallationPathRebindError('Rebind receipt must live outside the candidate')
    document, raw, old_prefix, committed, directories, bound = _load_manifest(manifest_path)
    inventory = {'files': committed, 'directories': sorted(directories)}
    manifest_sha256 = hashlib.sha256(raw).hexdigest()
    new_prefix = str(candidate / 'bodies')
    if old_prefix != new_prefix and (PurePosixPath(old_prefix).is_relative_to(new_prefix)
                                     or PurePosixPath(new_prefix).is_relative_to(old_prefix)):
        raise InstallationPathRebindError('Candidate prefixes overlap')
    catalog = candidate / 'catalog.sqlite3'
    part = candidate / PART
    bodies = candidate / 'bodies'
    pin = document['catalog_sha256']
    try:
        with control.lock(candidate, exclusive=True) as lease:
            lease_identity = os.fstat(lease)
            entries = {entry.name: entry for entry in candidate.iterdir()}
            if part.name in entries:
                _discard_part(part)
                _sync_directory(candidate)
            entries = {entry.name: entry for entry in candidate.iterdir()}
            backup = entries.pop(BACKUP, None)
            if backup is not None:
                info = backup.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_nlink not in (1, 2):
                    raise InstallationPathRebindError('Interrupted rebind backup is invalid')
                _, fact = files._read(backup, private=True, links=info.st_nlink)
                if fact['sha256'] != pin:
                    raise InstallationPathRebindError('Interrupted rebind backup changed')
            for name, entry in entries.items():
                if name in ENTRIES:
                    continue
                if _WORK.fullmatch(name):
                    files._inventory(entry, private=True)
                    continue
                raise InstallationPathRebindError('Unknown candidate entry')
            if not ENTRIES <= set(entries):
                raise InstallationPathRebindError('Candidate is incomplete')
            files._check(bodies.lstat(), directory=True, private=True)
            in_tree, _ = files._read(candidate / 'manifest.json', limit=files.MAX_JSON, private=True)
            if in_tree != raw:
                raise InstallationPathRebindError('Candidate manifest disagrees with the supplied receipt')
            current = sql._file_digest(catalog)
            rewrote = False
            if current == pin:
                after = pin
                if backup is not None:
                    # Crash between backup link and publication: stale backup.
                    backup.unlink()
                    _sync_directory(candidate)
                    backup = None
                if old_prefix != new_prefix:
                    _rewrite_copy(catalog, part, old=old_prefix, new=new_prefix,
                                  committed=committed, directories=directories, pin=pin)
                    _verify_bodies(bodies, inventory)
                    backup = candidate / BACKUP
                    os.link(catalog, backup)
                    os.replace(part, catalog)
                    _sync_directory(candidate)
                    rewrote = True
                    after = sql._file_digest(catalog)
            elif backup is not None:
                # Crash between Catalog publication and receipt: legitimacy is
                # re-derived, never assumed.  Only the deterministic rewrite of
                # the pinned backup bytes may equal the current Catalog.
                _, _, derived = _rewrite_copy(backup, part, old=old_prefix, new=new_prefix,
                                              committed=committed, directories=directories, pin=pin)
                if derived != current:
                    raise InstallationPathRebindError('Candidate Catalog drifted after publication')
                _discard_part(part)
                _sync_directory(candidate)
                after = current
            else:
                if not receipt_path.exists():
                    raise InstallationPathRebindError('Candidate Catalog drifted from its committed bytes')
                after = current
            try:
                if _after_publish is not None:
                    _after_publish()
                verify = sql._connect(catalog)
                try:
                    rows, _, counts = _scan(verify, old=new_prefix, new=old_prefix,
                                            committed=committed, directories=directories)
                finally:
                    verify.close()
                if len(rows) != bound:
                    raise InstallationPathRebindError('Candidate documents disagree with its body bindings')
                _verify_bodies(bodies, inventory)
                if sql._file_digest(catalog) != after:
                    raise InstallationPathRebindError('Candidate Catalog changed after publication')
                if files._read(manifest_path, limit=files.MAX_JSON, private=True)[0] != raw:
                    raise InstallationPathRebindError('Candidate receipt changed during rebind')
                if control.identity(candidate) != stage_identity:
                    raise InstallationPathRebindError('Candidate identity changed')
                lock_identity = (candidate / '.writer-authority.lock').lstat()
                if (lock_identity.st_dev, lock_identity.st_ino) != (lease_identity.st_dev, lease_identity.st_ino):
                    raise InstallationPathRebindError('Candidate control identity changed')
                receipt = {'format': FORMAT, 'state': STATE, 'candidate': str(candidate),
                           'manifest_sha256': manifest_sha256,
                           'old_prefix': old_prefix, 'new_prefix': new_prefix,
                           'rebound_fields': counts, 'documents': len(rows),
                           'catalog_sha256_before': pin, 'catalog_sha256_after': after,
                           'bodies_verified': len(committed), 'installation_path_rebound': True,
                           **{flag: False for flag in FLAGS}}
                data = control.encoded(receipt)
                if len(data) > _MAX_RECEIPT:
                    raise InstallationPathRebindError('Rebind receipt exceeds its budget')
                existed = _publish_receipt(receipt_path, data)
            except Exception:
                if rewrote and backup is not None:
                    os.replace(backup, catalog)
                    _sync_directory(candidate)
                raise
            if backup is not None:
                backup.unlink()
                _sync_directory(candidate)
    except Exception:
        if part.exists() or part.is_symlink():
            files._check(part.lstat(), private=True)
            part.unlink()
        raise
    idempotent = existed and not rewrote
    return dict(receipt, idempotent=idempotent, receipt_sha256=hashlib.sha256(data).hexdigest())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('candidate', 'manifest', 'output'):
        parser.add_argument('--' + name, required=True)
    args = parser.parse_args(argv)
    try:
        result = rebind_installation_paths(args.candidate, args.manifest, args.output)
    except Exception:
        print(json.dumps({'format': FORMAT, 'status': 'error', 'error_code': 'installation_path_rebind_rejected',
                          'installation_path_rebound': False, **{flag: False for flag in FLAGS}}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
