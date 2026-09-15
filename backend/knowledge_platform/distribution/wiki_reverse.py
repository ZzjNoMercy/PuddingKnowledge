"""Materialize an inactive legacy Wiki brain root from owned Knowledge state.

No writer is switched. The candidate restores the archived brain bytes plus
every committed post-migration Wiki publication as legacy receipts; relocating
or activating it requires a separate audited step.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile

import yaml

from . import wiki_archive as files, sqlite_reverse_delta as sql
from .document_reverse import _copy
from .wiki_reverse_plan import FORMAT, plan_wiki_reverse
from .wiki_schema_evidence import verify_schema_evidence, MAX_EVIDENCE, BRAIN as SCHEMA_BRAIN
from ..local import writer_authority as control
from ..local.catalog import _TITLE_RE
from ..local.migrated_wiki import _identity_ids
from ..local.wiki_authoring_projection import collection_id as _active_collection_id
from ..local.wiki_lineage import project_wiki_lineage
from ..local.wiki_raw import project_raw_assets
from ..local.workspace_freeze import _sync_directory
from ..wiki.lint import _StrictLoader

_COMPLETE = 'verified_inactive_wiki'
_EXPORT_FORMAT = 'puddingknowledge-frozen-workspace-export/v1'
_ASSET_SOURCES = ('local_published_wiki', 'local_wiki_raw', 'local_wiki_compilation', 'local_wiki_authoring')
_STATE_TABLES = ('knowledge_local_wiki_compilations', 'knowledge_local_wiki_schema_pages',
                 'knowledge_local_wiki_schema_log', 'knowledge_wiki_authoring_state',
                 'knowledge_wiki_authoring_pages', 'knowledge_wiki_authoring_commits')
_PAGE_LIMIT = 8 * 1024 * 1024


def _json_file(path):
    raw, _ = files._read(path, limit=files.MAX_JSON, private=True)
    return json.loads(raw, object_pairs_hook=control._unique)


def _write_bytes(destination, data, fact):
    part = Path(str(destination) + '.reverse-part')
    descriptor = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        files._check(os.fstat(descriptor), private=True)
        os.ftruncate(descriptor, 0)
        view = memoryview(data)
        while view:
            view = view[os.write(descriptor, view):]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    if {'sha256': hashlib.sha256(data).hexdigest(), 'size_bytes': len(data)} != fact:
        raise ValueError('Reverse write commitment mismatch')
    os.replace(part, destination)
    _sync_directory(destination.parent)


def _page_title(text, fallback):
    match = _TITLE_RE.match(text[:64 * 1024])
    if match:
        for line in match.group('frontmatter').splitlines():
            key, separator, value = line.partition(':')
            if separator and key.strip() == 'title' and value.strip():
                return value.strip().strip('\'"')[:500]
    return fallback[:500]


def _schema_identity(evidence, bundle):
    raw, _ = files._read(evidence / 'archive' / SCHEMA_BRAIN, private=True, limit=1024 * 1024)
    value = yaml.load(raw.decode('utf-8'), Loader=_StrictLoader)
    if not isinstance(value, dict):
        raise ValueError('Invalid Wiki schema brain')
    schema_id = value.get('schema_id')
    version = value.get('bundle_version')
    if (not isinstance(schema_id, str) or not schema_id or len(schema_id) > 160
            or any(character in schema_id for character in '\n\r`')):
        raise ValueError('Invalid Wiki schema identity')
    if not isinstance(version, str) or version != bundle.bundle_version:
        raise ValueError('Wiki schema version mismatch')
    return schema_id, version


def _export_commitment(current):
    raw, _ = files._read(current / 'manifest.json', limit=files.MAX_JSON, private=True)
    value = json.loads(raw, object_pairs_hook=control._unique)
    if not isinstance(value, dict) or control.encoded(value) != raw:
        raise ValueError('Export manifest must be canonical')
    expected = {'format', 'plan', 'plan_sha256', 'state', 'activation_allowed', 'rollback_completed',
                'legacy_schema_converted', 'credential_continuity_verified',
                'normalized_databases', 'normalized_inventory'}
    if set(value) != expected or value['format'] != _EXPORT_FORMAT or value['state'] != 'verified_frozen_export' \
            or not isinstance(value['plan'], dict):
        raise ValueError('Workspace export is not a verified frozen export')
    if control.digest(value['plan']) != value['plan_sha256']:
        raise ValueError('Export plan commitment mismatch')
    if files._inventory(current / 'raw', private=True) != value['plan']['source_inventory']:
        raise ValueError('Export raw inventory mismatch')
    if files._inventory(current / 'normalized', private=True) != value['normalized_inventory']:
        raise ValueError('Export normalized inventory mismatch')
    databases = value['normalized_databases']
    if not isinstance(databases, dict) or not isinstance(databases.get('catalog'), dict):
        raise ValueError('Export Catalog report is missing')
    catalog = sql._path(current / 'normalized/catalog/catalog.sqlite3')
    digest = sql._file_digest(catalog)
    if databases['catalog'].get('normalized_catalog') != {'digest': 'sha256:' + digest, 'size': catalog.stat().st_size}:
        raise ValueError('Export Catalog commitment mismatch')
    return value, raw, catalog, digest


def _has_rows(connection, table, space):
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
        return False
    return connection.execute(f'SELECT 1 FROM {table} WHERE space_id=? LIMIT 1', (space,)).fetchone() is not None


def _inspect(copied, evidence, manifest, manifest_bytes, bundle):
    space, collection, _version = _identity_ids(manifest)
    connection = sqlite3.connect(copied, timeout=5)
    try:
        connection.execute('BEGIN')
        if connection.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise ValueError('Current Catalog integrity check failed')
        spaces = connection.execute('SELECT id,name,description,permissions_json FROM knowledge_spaces').fetchall()
        if len(spaces) != 1 or spaces[0][0] != space or tuple(spaces[0][1:]) != ('Migrated Wiki', '{}', '{}'):
            raise ValueError('Wiki Space identity mismatch')
        compiled_collection = 'collection_compiled_wiki_' + hashlib.sha256(space.encode()).hexdigest()[:32]
        active_collection = _active_collection_id(space)
        datasets = {row[0] for row in connection.execute('SELECT id FROM knowledge_datasets')}
        if not datasets <= {collection, compiled_collection, active_collection}:
            raise ValueError('Unknown Wiki domain Collection')
        pages = {relative[5:-3]: fact for relative, fact in manifest['files'].items()
                 if relative.startswith('wiki/') and relative.endswith('.md')
                 and relative not in ('wiki/index.md', 'wiki/log.md')}
        order = sorted(pages, key=lambda slug: tuple((slug + '.md').split('/')))
        page_ids = {slug: 'asset_wiki_' + hashlib.sha256(f'{space}:{slug}'.encode()).hexdigest()[:32] for slug in order}
        row = connection.execute('SELECT name,version,kind,description,asset_ids,semantic_asset_ids,capabilities,'
                                 'permissions_json,manifest_digest FROM knowledge_datasets WHERE id=?', (collection,)).fetchone()
        asset_ids = json.loads(row[4]) if row is not None and isinstance(row[4], str) else None
        capabilities = json.loads(row[6]) if row is not None and isinstance(row[6], str) else None
        if (row is None or tuple(row[:4]) != ('Migrated Wiki', '1', 'wiki', '') or asset_ids != [page_ids[slug] for slug in order]
                or row[5] != '[]' or capabilities != ['wiki_query'] or row[7] != '{}'
                or row[8] != 'sha256:' + hashlib.sha256(manifest_bytes).hexdigest()):
            raise ValueError('Wiki Collection baseline mismatch')
        assets = {}
        for record in connection.execute('SELECT id,space_id,kind,title,description,mime_type,source_type,source_uri,revision,'
                                         'content_digest,permissions_json,metadata_json FROM knowledge_assets'):
            if record[1] != space or record[6] not in _ASSET_SOURCES:
                raise ValueError('Unknown Wiki domain Asset')
            assets[record[0]] = record
        for slug in order:
            record = assets.get(page_ids[slug])
            if record is None or record[6] != 'local_published_wiki':
                raise ValueError('Wiki page Asset baseline mismatch')
            fact = pages[slug]
            data, actual = files._read(evidence / 'archive' / ('wiki/' + slug + '.md'), private=True, limit=_PAGE_LIMIT)
            if actual != fact:
                raise ValueError('Archived Wiki page changed')
            digest = 'sha256:' + fact['sha256']
            metadata = json.loads(record[11])
            if (tuple(record[2:11]) != ('wiki_page', _page_title(data.decode('utf-8'), slug.rsplit('/', 1)[-1]), '',
                                        'text/markdown', 'local_published_wiki',
                                        f'knowledge://spaces/{space}/assets/{page_ids[slug]}', digest, digest, '{}')
                    or metadata != {'published': True, 'wiki_slug': slug, 'bytes': fact['size_bytes']}):
                raise ValueError('Wiki page Asset baseline mismatch')
        projection = project_raw_assets(evidence, manifest, space)
        for asset_id, metadata in project_wiki_lineage(evidence, manifest, projection['assets']).items():
            projection['assets'][asset_id]['metadata'].update(metadata)
        raw_facts = projection['assets']
        if {asset_id for asset_id, record in assets.items() if record[6] == 'local_published_wiki'} != set(page_ids.values()):
            raise ValueError('Wiki page Asset set mismatch')
        if {asset_id for asset_id, record in assets.items() if record[6] == 'local_wiki_raw'} != set(raw_facts):
            raise ValueError('Wiki Raw Asset set mismatch')
        for asset_id, fact in raw_facts.items():
            record = assets[asset_id]
            metadata = json.loads(record[11])
            if (tuple(record[2:11]) != ('raw_snapshot', projection['titles'][asset_id], '', 'application/octet-stream',
                                        'local_wiki_raw', fact['source_uri'], fact['revision'], fact['content_digest'], '{}')
                    or metadata != fact['metadata']):
                raise ValueError('Wiki Raw Asset baseline mismatch')
        for table in _STATE_TABLES:
            if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() \
                    and connection.execute(f'SELECT 1 FROM {table} WHERE space_id != ? LIMIT 1', (space,)).fetchone():
                raise ValueError('Wiki state table hosts another Space')
        running = 0
        succeeded = set()
        if _has_rows(connection, 'knowledge_local_wiki_compilations', space):
            statuses = {row[0] for row in connection.execute(
                'SELECT DISTINCT status FROM knowledge_local_wiki_compilations WHERE space_id=?', (space,))}
            if not statuses <= {'succeeded', 'running'}:
                raise ValueError('Unknown Wiki compilation status')
            running = connection.execute("SELECT count(*) FROM knowledge_local_wiki_compilations WHERE space_id=? AND status='running'",
                                         (space,)).fetchone()[0]
            succeeded = {row[0] for row in connection.execute(
                "SELECT DISTINCT resource_uri FROM knowledge_local_wiki_compilations WHERE space_id=? AND status='succeeded'", (space,))}
        compiled_assets = sorted(asset_id for asset_id, record in assets.items() if record[6] == 'local_wiki_compilation')
        compiled_uris = {f'knowledge://spaces/{space}/assets/{asset_id}' for asset_id in compiled_assets}
        if succeeded != compiled_uris:
            raise ValueError('Committed Wiki compilation evidence mismatch')
        authoring_state = _has_rows(connection, 'knowledge_wiki_authoring_state', space)
        needs_schema = bool(compiled_assets) or authoring_state or _has_rows(connection, 'knowledge_local_wiki_schema_pages', space) \
            or _has_rows(connection, 'knowledge_local_wiki_schema_log', space)
        publication = None
        compilations = []
        if needs_schema:
            if bundle is None:
                raise ValueError('Committed Wiki state requires the admitted schema evidence')
            from ..local.wiki_schema_publication import SchemaPublication
            owned = {'schema_bundle': bundle, 'space_id': space, 'evidence_root': evidence,
                     'raw_bindings': {asset_id: evidence / 'archive' / 'raw' / fact['metadata']['snapshot_path']
                                      for asset_id, fact in raw_facts.items()},
                     'catalog': copied}
            publication = SchemaPublication(owned, copied)
            publication.initialize(connection)
            publication._current(connection)
            for slug, markdown, uri, receipt in connection.execute(
                    'SELECT slug,markdown,resource_uri,receipt_id FROM knowledge_local_wiki_schema_pages WHERE space_id=?', (space,)):
                logs = connection.execute('SELECT source_snapshot_id,source_digest FROM knowledge_local_wiki_schema_log '
                                          'WHERE space_id=? AND resource_uri=? AND slug=?', (space, uri, slug)).fetchall()
                attempts = connection.execute("SELECT updated_at FROM knowledge_local_wiki_compilations "
                                              "WHERE space_id=? AND resource_uri=? AND status='succeeded'", (space, uri)).fetchall()
                if len(logs) != 1 or not attempts or logs[0][0] not in raw_facts:
                    raise ValueError('Schema publication evidence is incomplete')
                compilations.append({'slug': slug, 'markdown': bytes(markdown).decode('utf-8'), 'resource_uri': uri,
                                     'receipt_id': receipt, 'snapshot_path': raw_facts[logs[0][0]]['metadata']['snapshot_path'],
                                     'content_digest': logs[0][1], 'updated_at': min(row[0] for row in attempts)})
        if compiled_collection in datasets:
            row = connection.execute('SELECT space_id,name,version,kind,description,asset_ids,semantic_asset_ids,capabilities,'
                                     'permissions_json,manifest_digest FROM knowledge_datasets WHERE id=?', (compiled_collection,)).fetchone()
            members = json.loads(row[5]) if isinstance(row[5], str) else None
            caps = json.loads(row[7]) if isinstance(row[7], str) else None
            if (tuple(row[:5]) != (space, 'Compiled Wiki ' + space, '1', 'wiki', 'local_wiki_compilation/v1')
                    or not isinstance(members, list) or set(members) != set(compiled_assets) or len(members) != len(set(members))
                    or row[6] != '[]' or caps != ['wiki_query'] or row[8] != '{}' or row[9] != ''):
                raise ValueError('Compiled Wiki Collection mismatch')
        authoring = None
        if authoring_state:
            if bundle is None:
                raise ValueError('Wiki authoring requires the admitted schema evidence')
            from ..local.wiki_authoring import WikiAuthoringStore
            store = WikiAuthoringStore(database=copied, space_id=space, bundle=bundle, raw_hashes=publication.raw,
                                       raw_manifest_sha256=publication.raw_manifest_sha256, catalog_projection=True)
            revision, committed_pages, index, log = store.read()
            commits = [dict(zip(('operation_id', 'request_digest', 'previous_revision', 'revision', 'retired_json',
                                 'patch_json', 'before_json', 'receipt_digest'), row))
                       for row in connection.execute('SELECT operation_id,request_digest,previous_revision,revision,retired_json,'
                                                     'patch_json,before_json,receipt_digest FROM knowledge_wiki_authoring_commits '
                                                     'WHERE space_id=?', (space,))]
            authoring = {'revision': revision, 'pages': committed_pages, 'index': index, 'log': log, 'commits': commits}
        elif (active_collection in datasets or any(record[6] == 'local_wiki_authoring' for record in assets.values())
                or _has_rows(connection, 'knowledge_wiki_authoring_pages', space)
                or _has_rows(connection, 'knowledge_wiki_authoring_commits', space)):
            raise ValueError('Active Wiki projection lacks authoring state')
        return compilations, authoring, running
    finally:
        connection.close()


def _source_text(source, relative, fact):
    data, actual = files._read(source / relative, limit=files.MAX_FILE)
    if actual != fact:
        raise ValueError('Reverse source changed')
    return data.decode('utf-8')


def prepare_wiki_reverse(source_snapshot, baseline_archive, current_workspace, output, *, _after_copy=None):
    source, baseline, current, stage = [files._path(value) for value in
                                        (source_snapshot, baseline_archive, current_workspace, output)]
    files._check(source.stat(), directory=True)
    inputs = (source, baseline, current)
    if len({str(path) for path in inputs}) != 3 or any(
            first == second or first.is_relative_to(second) or second.is_relative_to(first)
            for index, first in enumerate(inputs) for second in inputs[index + 1:]):
        raise ValueError('Reverse inputs must be distinct and disjoint')
    if any(stage == path or stage.is_relative_to(path) or path.is_relative_to(stage) for path in inputs):
        raise ValueError('Reverse output overlaps input')
    manifest = files.verify_archive(baseline)
    manifest_bytes, _ = files._read(baseline / 'manifest.json', private=True, limit=files.MAX_JSON)
    inventory = files._inventory(source)
    if inventory['files'] != manifest['files'] or inventory['directories'] != manifest['directories']:
        raise ValueError('Source snapshot drifted from the baseline Wiki archive')
    files._raw(source, inventory)
    export_manifest, export_bytes, catalog, catalog_digest = _export_commitment(current)
    evidence = current / 'raw/wiki-evidence'
    if files.verify_archive(evidence) != manifest:
        raise ValueError('Workspace Wiki evidence drifted from the baseline archive')
    bundle = schema_id = schema_version = None
    schema_path = current / 'raw/wiki-schema.json'
    if schema_path.exists():
        data, _ = files._read(schema_path, private=True, limit=MAX_EVIDENCE)
        bundle = verify_schema_evidence(data, evidence)
        schema_id, schema_version = _schema_identity(evidence, bundle)
    with tempfile.TemporaryDirectory(prefix='wiki-reverse-inspect-') as temporary:
        copied = Path(temporary).resolve() / 'catalog.sqlite3'
        if sql._file_digest(catalog, copy_to=copied) != catalog_digest:
            raise ValueError('Current Catalog changed during inspection')
        compilations, authoring, running = _inspect(copied, evidence, manifest, manifest_bytes, bundle)
    archive_pages = {}; archive_index = archive_log = ''
    raw_hashes = {}; raw_manifest_sha256 = ''; historical = []
    if compilations or authoring is not None:
        for relative, fact in manifest['files'].items():
            if relative.startswith('wiki/') and relative.endswith('.md'):
                text = _source_text(source, relative, fact)
                if relative == 'wiki/index.md':
                    archive_index = text
                elif relative == 'wiki/log.md':
                    archive_log = text
                else:
                    archive_pages[relative[5:-3]] = text
        raw_data, raw_fact = files._read(source / 'raw/manifest.jsonl', limit=files.MAX_JSON)
        if inventory['files'].get('raw/manifest.jsonl') != raw_fact:
            raise ValueError('Reverse source changed')
        raw_manifest_sha256 = hashlib.sha256(raw_data).hexdigest()
        for line in raw_data.decode('utf-8').splitlines():
            if line.strip():
                record = files._json(line.encode())
                raw_hashes[record['snapshot_path']] = record['sha256']
        for relative, fact in manifest['files'].items():
            if relative.startswith('.puddingclaw/jobs/wiki-') and relative.endswith('.json') and relative.count('/') == 2:
                data, actual = files._read(source / relative, limit=files.MAX_JSON)
                if actual != fact:
                    raise ValueError('Reverse source changed')
                try:
                    record = files._json(data)
                except ValueError:
                    continue
                if isinstance(record, dict) and isinstance(record.get('published_at'), str):
                    historical.append(record['published_at'])
    plan = plan_wiki_reverse(source_files=inventory['files'], source_directories=inventory['directories'],
                             archive_pages=archive_pages, archive_index=archive_index, archive_log=archive_log,
                             raw_hashes=raw_hashes, raw_manifest_sha256=raw_manifest_sha256,
                             historical_published_at=historical, compilations=compilations,
                             authoring=authoring, bundle=bundle, schema_id=schema_id, schema_version=schema_version)
    output_inventory = {'files': plan['files'], 'directories': plan['directories']}
    if not stage.exists():
        stage.mkdir(mode=0o700)
        _sync_directory(stage.parent)
    stage_identity = control.identity(stage)
    with control.lock(stage, exclusive=True) as lease:
        lease_identity = os.fstat(lease)
        plan_record = {'format': FORMAT, 'installation_id': manifest['installation_id'],
                       'source_revision': manifest['source_revision'],
                       'inputs': {'source': {'path': str(source), 'inventory_sha256': control.digest(inventory)},
                                  'baseline_archive': {'path': str(baseline),
                                                       'manifest_sha256': hashlib.sha256(manifest_bytes).hexdigest()},
                                  'current_workspace': {'path': str(current),
                                                        'manifest_sha256': hashlib.sha256(export_bytes).hexdigest(),
                                                        'catalog_sha256': catalog_digest}},
                       'output_identity': stage_identity, 'output_inventory': output_inventory,
                       'delta': plan['delta'], 'receipts': plan['receipts']}
        base = {'format': FORMAT, 'plan': plan_record, 'state': 'copying', 'activation_allowed': False,
                'rollback_completed': False, 'credential_continuity_verified': False,
                'indexes_rebuilt': False, 'installation_path_rebound': False}
        if len(control.encoded(base)) > files.MAX_JSON:
            raise ValueError('Reverse manifest budget exceeded')
        marker = stage / 'manifest.json'
        previous = None
        if marker.exists() or marker.is_symlink():
            previous = _json_file(marker)
            expected_keys = set(base) | ({'receipt'} if previous.get('state') == _COMPLETE else set())
            if (set(previous) != expected_keys or previous['state'] not in ('copying', _COMPLETE)
                    or any(not sql._json(previous[key]) == sql._json(value) for key, value in base.items() if key != 'state')):
                raise ValueError('Reverse plan changed')
        elif any(entry.name != '.writer-authority.lock' for entry in stage.iterdir()):
            raise ValueError('Unowned reverse output')
        else:
            control._replace(marker, base)
        complete = previous is not None and previous['state'] == _COMPLETE
        for entry in stage.iterdir():
            if entry.name in {'.writer-authority.lock', 'manifest.json', 'brain'}:
                continue
            if re.fullmatch(r'\.manifest\.json\.tmp-[a-f0-9]{16}', entry.name):
                files._check(entry.lstat(), private=True)
                continue
            raise ValueError('Unknown reverse output entry')
        brain = stage / 'brain'
        if complete and files._inventory(brain, private=True) != output_inventory:
            raise ValueError('Completed reverse brain changed')
        if not brain.exists():
            brain.mkdir(mode=0o700)
            _sync_directory(stage)
        files._check(brain.lstat(), directory=True, private=True)
        partial = files._inventory(brain, private=True)
        if not set(partial['directories']) <= set(plan['directories']):
            raise ValueError('Unknown reverse brain directory')
        for name in partial['files']:
            if name not in plan['files'] and not (name.endswith('.reverse-part')
                                                  and name.removesuffix('.reverse-part') in plan['files']):
                raise ValueError('Unknown reverse brain file')
        for directory in sorted(plan['directories'], key=lambda name: (len(name.split('/')), name)):
            destination = brain / directory
            if not destination.exists():
                destination.mkdir(mode=0o700)
                _sync_directory(destination.parent)
            files._check(destination.lstat(), directory=True, private=True)
        if not complete:
            for name, fact in sorted(plan['files'].items()):
                target = brain / name
                if target.exists():
                    if files._read(target, private=True)[1] != fact:
                        raise ValueError('Reverse brain file changed')
                elif name in plan['writes']:
                    _write_bytes(target, plan['writes'][name], fact)
                    if _after_copy:
                        _after_copy(name)
                else:
                    _copy(source / name, target, fact)
                    if _after_copy:
                        _after_copy(name)
            if files._inventory(brain, private=True) != output_inventory:
                raise ValueError('Reverse brain inventory mismatch')
        receipt = {'delta': plan['delta'], 'receipts': plan['receipts'], 'running_compilations': running}
        if complete and sql._json(previous['receipt']) != sql._json(receipt):
            raise ValueError('Completed reverse receipt changed')
        result = previous if complete else {**base, 'state': _COMPLETE, 'receipt': receipt}
        if files._inventory(source) != inventory:
            raise ValueError('Reverse source changed')
        if files.verify_archive(baseline) != manifest:
            raise ValueError('Baseline Wiki archive changed')
        if files._read(current / 'manifest.json', limit=files.MAX_JSON, private=True)[0] != export_bytes:
            raise ValueError('Workspace export changed')
        if files._inventory(current / 'raw', private=True) != export_manifest['plan']['source_inventory']:
            raise ValueError('Export raw content changed')
        if files._inventory(current / 'normalized', private=True) != export_manifest['normalized_inventory']:
            raise ValueError('Export normalized content changed')
        if sql._file_digest(catalog) != catalog_digest:
            raise ValueError('Export Catalog changed')
        if files.verify_archive(evidence) != manifest:
            raise ValueError('Workspace Wiki evidence changed')
        if files._inventory(brain, private=True) != output_inventory:
            raise ValueError('Reverse output changed before commit')
        current_lock = (stage / '.writer-authority.lock').lstat()
        if (control.identity(stage) != stage_identity
                or (current_lock.st_dev, current_lock.st_ino) != (lease_identity.st_dev, lease_identity.st_ino)):
            raise ValueError('Reverse control identity changed')
        if len(control.encoded(result)) > files.MAX_JSON:
            raise ValueError('Completed reverse manifest exceeds budget')
        if not complete:
            control._replace(marker, result)
        return {'format': FORMAT, 'state': result['state'], 'plan_sha256': control.digest(plan_record),
                'installation_id': manifest['installation_id'], 'source_revision': manifest['source_revision'],
                'delta': result['receipt']['delta'], 'receipts_written': len(result['receipt']['receipts']),
                'job_ids': [entry['job_id'] for entry in result['receipt']['receipts']],
                'running_compilations': result['receipt']['running_compilations'],
                'file_count': len(plan['files']), 'idempotent': complete,
                'wiki_domain_reversed': True, 'raw_domain_unchanged': True, 'schema_unchanged': True,
                'activation_allowed': False, 'rollback_completed': False,
                'credential_continuity_verified': False, 'indexes_rebuilt': False,
                'installation_path_rebound': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('source-snapshot', 'baseline-archive', 'current-workspace', 'output'):
        parser.add_argument('--' + name, required=True)
    args = vars(parser.parse_args(argv))
    try:
        result = prepare_wiki_reverse(**args)
    except Exception:
        print(json.dumps({'format': FORMAT, 'status': 'error', 'error_code': 'wiki_reverse_rejected',
                          'activation_allowed': False}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
