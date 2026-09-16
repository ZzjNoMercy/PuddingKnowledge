"""Classify non-core Platform Catalog deltas inside a frozen rollback window.

Legacy Claw never had the non-core Catalog tables, so rollback never
materializes old-schema rows for them. Each table is either proven canonically
unchanged between the target snapshots, or proven captured by a verified
frozen workspace export binding the exact target-after bytes for a later
re-cutover. Job queues must be fully drained. This consumes snapshot evidence;
no writer is switched and nothing here is an installation rollback.
"""
from contextlib import ExitStack
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

from . import sqlite_reverse_delta as sql
from . import wiki_archive as files
from .catalog_snapshot import _path

FORMAT = 'puddingknowledge-other-catalog-reverse/v1'
STATE = 'verified_other_catalog_disposition'
EXPORT_FORMAT = 'puddingknowledge-frozen-workspace-export/v1'
EXPORT_STATE = 'verified_frozen_export'
CORE = ('knowledge_spaces', 'knowledge_assets', 'knowledge_datasets')
VERSIONS = 'knowledge_catalog_schema_versions'
NON_CORE = (
    'knowledge_connectors', 'knowledge_database_connectors', 'knowledge_source_items',
    'knowledge_sync_runs', 'knowledge_credentials', 'knowledge_credential_grants',
    'knowledge_oauth_sessions', 'knowledge_web_captures', 'knowledge_ingestion_jobs',
    'knowledge_ingestion_events', 'knowledge_structured_assets', 'knowledge_query_results',
    'knowledge_query_result_scopes', 'knowledge_processing_jobs', 'knowledge_processing_events',
    'knowledge_authoring_jobs', 'knowledge_authoring_events', 'knowledge_notification_events',
    'knowledge_notification_event_scopes', 'knowledge_collection_bindings',
)
UNCHANGED = 'verified_unchanged'
CAPTURED = 'captured_in_frozen_export'
DISPOSITIONS = (UNCHANGED, CAPTURED)
# Terminal-status allowlists; any other value in the target-after image refuses
# the rollback. Derivation:
# - knowledge_ingestion_jobs: the only writers (local/files.py and
#   local/read_later.py) only ever write queued -> running -> succeeded|failed.
# - knowledge_processing_jobs: catalog/processing_job_rehearsal.py lines 44-45.
# - knowledge_authoring_jobs: catalog/authoring_job_rehearsal.py lines 43-51;
#   the waiting_for_* states still await a confirmation decision.
# - knowledge_oauth_sessions: local/feishu_oauth.py (pending/exchanging are
#   in-flight grants) and catalog/credential_rehearsal.py line 195 (expired
#   marks an abandoned pending handshake).
TERMINAL_STATUS = {
    'knowledge_ingestion_jobs': frozenset(('succeeded', 'failed')),
    'knowledge_processing_jobs': frozenset(('succeeded', 'failed', 'cancelled')),
    'knowledge_authoring_jobs': frozenset(('published', 'failed', 'cancelled')),
    'knowledge_oauth_sessions': frozenset(('consumed', 'superseded', 'failed', 'revoked', 'expired')),
}
FLAGS = ('indexes_rebuilt', 'rollback_completed', 'activation_allowed', 'installation_cutover_performed')
_MAX_RECEIPT = 1024 * 1024
_MAX_MANIFEST = 32 * 1024 * 1024
_HEX = re.compile(r'[0-9a-f]{64}')


def _encode(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False) + '\n').encode()


def _unique(items):
    result = {}
    for key, item in items:
        if key in result: raise ValueError('Duplicate protocol key')
        result[key] = item
    return result


def _read_canonical(path, limit):
    raw, _ = files._read(path, limit=limit, private=True)
    value = json.loads(raw, object_pairs_hook=_unique)
    if not isinstance(value, dict) or _encode(value) != raw:
        raise ValueError('Protocol evidence must be canonical')
    return value, raw


def _hexdigest(value):
    return isinstance(value, str) and _HEX.fullmatch(value) is not None


def _export_root(value):
    root = _path(value)
    if not root.is_dir(): raise ValueError('Frozen export is unavailable')
    files._check(root.stat(), directory=True, private=True)
    return root


def _verify_frozen_export(export, after_sha256):
    manifest, raw = _read_canonical(export / 'manifest.json', _MAX_MANIFEST)
    if manifest.get('format') != EXPORT_FORMAT or manifest.get('state') != EXPORT_STATE:
        raise ValueError('Frozen export is not a verified receipt')
    if manifest.get('activation_allowed') is not False or manifest.get('rollback_completed') is not False:
        raise ValueError('Frozen export is not inert')
    plan = manifest.get('plan')
    inventory = plan.get('source_inventory') if isinstance(plan, dict) else None
    exported = inventory.get('files') if isinstance(inventory, dict) else None
    fact = exported.get('catalog.sqlite3') if isinstance(exported, dict) else None
    if not isinstance(fact, dict) or set(fact) != {'sha256', 'size_bytes'}:
        raise ValueError('Frozen export has no pinned Catalog fact')
    catalog = export / 'raw' / 'catalog.sqlite3'
    # A suspended Catalog with live sidecars is not fully captured by its raw
    # main-file bytes; quiesce and re-export before disposition.
    for suffix in ('-wal', '-shm', '-journal'):
        sidecar = _path(str(catalog) + suffix)
        if sidecar.exists() or sidecar.is_symlink():
            raise ValueError('Frozen export Catalog is not quiesced')
    _, actual = files._read(catalog, limit=files.MAX_FILE, private=True)
    if actual != fact: raise ValueError('Frozen export Catalog changed')
    if actual['sha256'] != after_sha256:
        raise ValueError('Frozen export does not capture the target-after Catalog')
    return {'manifest_sha256': hashlib.sha256(raw).hexdigest(), 'catalog_sha256': actual['sha256']}


def _publish(path, data):
    part = path.parent / (path.name + '.part')
    if part.exists() or part.is_symlink():
        files._read(part, limit=_MAX_RECEIPT, private=True)
        part.unlink()
    if path.exists():
        existing, _ = files._read(path, limit=_MAX_RECEIPT, private=True)
        if existing != data: raise ValueError('Existing disposition receipt disagrees')
        return True
    fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data); stream.flush(); os.fsync(stream.fileno())
    os.link(part, path)
    part.unlink()
    descriptor = os.open(path.parent, os.O_RDONLY)
    try: os.fsync(descriptor)
    finally: os.close(descriptor)
    return False


def build_other_catalog_reverse(target_before, target_after, frozen_export, output, *, _after_snapshot=None):
    before, after = [sql._path(path) for path in (target_before, target_after)]
    export = _export_root(frozen_export)
    destination = _path(output)
    if destination.exists() and not destination.is_file():
        raise ValueError('Invalid disposition receipt path')
    if not destination.parent.is_dir(): raise ValueError('Receipt parent is unavailable')
    if (before.stat().st_dev, before.stat().st_ino) == (after.stat().st_dev, after.stat().st_ino):
        raise ValueError('Distinct snapshot inputs required')
    with ExitStack() as stack:
        private = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='.other-reverse-', dir=destination.parent)))
        copies = [private / 'before.sqlite3', private / 'after.sqlite3']
        digests = [sql._file_digest(path, copy_to=copied) for path, copied in zip((before, after), copies)]
        connections = [sql._connect(path) for path in copies]
        for connection in connections:
            stack.callback(connection.close)
            if connection.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                raise ValueError('Invalid input database')
        pre, post = [sql._snapshot(connection) for connection in connections]
        if sql._layout(pre) != sql._layout(post):
            raise ValueError('Target schema changed')
        known = set(CORE) | set(NON_CORE) | {VERSIONS}
        for snapshot in (pre, post):
            unknown = sorted(set(snapshot['tables']) - known)
            if unknown: raise ValueError('Unknown target domain table: ' + ','.join(unknown))
        if VERSIONS in pre['tables'] and pre['tables'][VERSIONS] != post['tables'][VERSIONS]:
            raise ValueError('Catalog schema versioning changed')
        # Queue drain is judged on the after image itself, not on the delta:
        # a job left non-terminal at freeze refuses the whole rollback.
        for name in sorted(TERMINAL_STATUS):
            table = post['tables'].get(name)
            if table is None: continue
            status = table['columns'].index('status')
            if any(row[status] not in TERMINAL_STATUS[name] for row in table['rows']):
                raise ValueError('Job queue is not drained: ' + name)
        if _after_snapshot is not None: _after_snapshot()
        tables = []
        for name in sorted(set(pre['tables']) & set(NON_CORE)):
            old, new = pre['tables'][name], post['tables'][name]
            tables.append({'table': name, 'rows_before': len(old['rows']), 'rows_after': len(new['rows']),
                           'digest_before': sql._digest(old['rows']), 'digest_after': sql._digest(new['rows']),
                           'disposition': UNCHANGED if old == new else CAPTURED})
        evidence = _verify_frozen_export(export, digests[1])
        receipt = {'format': FORMAT, 'state': STATE, 'schema_versions_identical': True,
                   'target_before_sha256': digests[0], 'target_after_sha256': digests[1],
                   'frozen_export': evidence, 'tables': tables, **{flag: False for flag in FLAGS}}
        data = _encode(receipt)
        if len(data) > _MAX_RECEIPT: raise ValueError('Disposition receipt exceeds budget')
        for path, digest in zip((before, after), digests):
            sql._path(path)
            if sql._file_digest(path) != digest: raise ValueError('Input snapshot changed')
        if _verify_frozen_export(export, digests[1]) != evidence:
            raise ValueError('Frozen export changed during disposition')
        idempotent = _publish(destination, data)
        return dict(receipt, idempotent=idempotent, receipt_sha256=hashlib.sha256(data).hexdigest())


def load_disposition(value):
    receipt, _ = _read_canonical(value, _MAX_RECEIPT)
    if receipt.get('format') != FORMAT or receipt.get('state') != STATE:
        raise ValueError('Disposition receipt is not verified')
    if any(receipt.get(flag) is not False for flag in FLAGS):
        raise ValueError('Disposition receipt is not inert')
    if receipt.get('schema_versions_identical') is not True:
        raise ValueError('Disposition receipt does not verify schema versioning')
    before_sha256, after_sha256 = receipt.get('target_before_sha256'), receipt.get('target_after_sha256')
    if not _hexdigest(before_sha256) or not _hexdigest(after_sha256):
        raise ValueError('Disposition receipt identity is invalid')
    evidence = receipt.get('frozen_export')
    if not isinstance(evidence, dict) or not _hexdigest(evidence.get('manifest_sha256')) or evidence.get('catalog_sha256') != after_sha256:
        raise ValueError('Disposition receipt evidence is invalid')
    entries = receipt.get('tables')
    if not isinstance(entries, list) or len(entries) > len(NON_CORE):
        raise ValueError('Disposition receipt tables are invalid')
    covered = {}
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {'table', 'rows_before', 'rows_after', 'digest_before', 'digest_after', 'disposition'}:
            raise ValueError('Disposition receipt entry is invalid')
        name = entry['table']
        if name not in NON_CORE or name in covered or entry['disposition'] not in DISPOSITIONS:
            raise ValueError('Disposition receipt entry is invalid')
        if type(entry['rows_before']) is not int or entry['rows_before'] < 0 or type(entry['rows_after']) is not int or entry['rows_after'] < 0:
            raise ValueError('Disposition receipt entry is invalid')
        before_digest, after_digest = entry['digest_before'], entry['digest_after']
        for digest in (before_digest, after_digest):
            if not isinstance(digest, str) or not digest.startswith('sha256:') or not _hexdigest(digest[7:]):
                raise ValueError('Disposition receipt entry is invalid')
        same = before_digest == after_digest
        if (entry['disposition'] == UNCHANGED) != same or (same and entry['rows_before'] != entry['rows_after']):
            raise ValueError('Disposition receipt contradicts itself')
        covered[name] = entry
    return {'target_before_sha256': before_sha256, 'target_after_sha256': after_sha256, 'tables': covered}


def verify_other_catalog_disposition(value, *, changed, before, after, before_sha256, after_sha256):
    """Admit a non-core delta only when a verified receipt covers exactly it."""
    receipt = load_disposition(value)
    if receipt['target_before_sha256'] != before_sha256 or receipt['target_after_sha256'] != after_sha256:
        raise ValueError('Unmapped target domain changed')
    if {name for name, entry in receipt['tables'].items() if entry['disposition'] == CAPTURED} != set(changed):
        raise ValueError('Unmapped target domain changed')
    for name in changed:
        entry = receipt['tables'][name]
        old, new = before['tables'][name], after['tables'][name]
        if (entry['rows_before'] != len(old['rows']) or entry['rows_after'] != len(new['rows'])
                or entry['digest_before'] != sql._digest(old['rows']) or entry['digest_after'] != sql._digest(new['rows'])):
            raise ValueError('Unmapped target domain changed')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('target-before', 'target-after', 'frozen-export', 'output'):
        parser.add_argument('--' + name, required=True)
    args = parser.parse_args(argv)
    try:
        result = build_other_catalog_reverse(args.target_before, args.target_after, args.frozen_export, args.output)
    except Exception:
        print(json.dumps({'format': FORMAT, 'status': 'error', 'error_code': 'other_catalog_reverse_rejected',
                          'activation_allowed': False, 'rollback_completed': False}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
