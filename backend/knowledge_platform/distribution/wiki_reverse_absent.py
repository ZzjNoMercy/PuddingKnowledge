"""Attest that a frozen owned Knowledge workspace export contains no Wiki domain.

This is the proof-of-absence counterpart of distribution/wiki_reverse.py for
installations that never had a Wiki domain. The same verified frozen workspace
export the active Wiki reverse consumes is re-verified, then the normalized
Catalog must prove empty: zero Spaces, zero Collections, zero Assets and zero
rows in every Wiki state table the active path inspects. Any Wiki row anywhere,
any unknown table and any Wiki evidence member in the export refuses; this tool
never runs as a fallback for a failed Wiki reverse. No writer is switched and
nothing is activated.
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

from . import wiki_archive as files, sqlite_reverse_delta as sql
from .wiki_reverse import _STATE_TABLES, _export_commitment, _json_file
from ..local import writer_authority as control
from ..local.workspace_freeze import _sync_directory

FORMAT = 'puddingknowledge-wiki-reverse-absent/v1'
STATE = 'verified_absent_wiki'
# The active path's domain surface (distribution/wiki_reverse.py _inspect): its
# exact column selections on the core tables, then the Wiki state tables.
_DOMAIN_QUERIES = (
    ('knowledge_spaces', 'id,name,description,permissions_json'),
    ('knowledge_datasets', 'id'),
    ('knowledge_assets', 'id,space_id,kind,title,description,mime_type,source_type,source_uri,revision,'
                         'content_digest,permissions_json,metadata_json'),
)
INSPECTED_TABLES = tuple(table for table, _columns in _DOMAIN_QUERIES) + _STATE_TABLES
# Known Catalog schema: knowledge_platform/catalog/migrations.py v1-v12 tables
# plus its version journal, and the Wiki state tables above. Anything outside
# this set is schema drift and refuses; a newer schema must extend it
# deliberately rather than being admitted silently.
_CORE_SCHEMA = frozenset((
    'knowledge_spaces', 'knowledge_assets', 'knowledge_datasets',
    'knowledge_connectors', 'knowledge_source_items', 'knowledge_sync_runs',
    'knowledge_credentials', 'knowledge_credential_grants', 'knowledge_oauth_sessions',
    'knowledge_web_captures', 'knowledge_ingestion_jobs', 'knowledge_ingestion_events',
    'knowledge_structured_assets', 'knowledge_query_results', 'knowledge_processing_jobs',
    'knowledge_processing_events', 'knowledge_authoring_jobs', 'knowledge_authoring_events',
    'knowledge_database_connectors', 'knowledge_notification_events',
    'knowledge_collection_bindings', 'knowledge_notification_event_scopes',
    'knowledge_query_result_scopes', 'knowledge_catalog_schema_versions',
))
_KNOWN_TABLES = _CORE_SCHEMA | frozenset(_STATE_TABLES)


def _inspect_absent(copied):
    connection = sqlite3.connect(copied, timeout=5)
    try:
        connection.execute('BEGIN')
        if connection.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise ValueError('Current Catalog integrity check failed')
        names = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        if not names <= _KNOWN_TABLES:
            raise ValueError('Unknown Catalog table')
        tables = []
        for table, columns in _DOMAIN_QUERIES:
            if table not in names:
                raise ValueError('Catalog schema is incomplete')
            if connection.execute(f'SELECT {columns} FROM {table} LIMIT 1').fetchone() is not None:
                raise ValueError('Wiki domain is present in the Catalog')
            tables.append({'table': table, 'present': True, 'rows': 0})
        for table in _STATE_TABLES:
            present = table in names
            if present and connection.execute(f'SELECT 1 FROM {table} LIMIT 1').fetchone() is not None:
                raise ValueError('Wiki domain is present in the Catalog')
            tables.append({'table': table, 'present': present, 'rows': 0})
        return tables
    finally:
        connection.close()


def attest_wiki_absent(current_workspace, output, *, source_revision):
    """Verify a frozen export holds no Wiki domain and publish the attestation.

    An exact retry finds the byte-identical receipt and reports idempotent; a
    disagreeing existing receipt refuses.
    """
    current, stage = [files._path(value) for value in (current_workspace, output)]
    if not isinstance(source_revision, str) or not files.TOKEN.fullmatch(source_revision):
        raise ValueError('Invalid source revision')
    if stage == current or stage.is_relative_to(current) or current.is_relative_to(stage):
        raise ValueError('Attestation output overlaps input')
    export_manifest, export_bytes, catalog, catalog_digest = _export_commitment(current)
    for name in ('wiki-evidence', 'wiki-schema.json'):
        member = current / 'raw' / name
        if member.exists() or member.is_symlink():
            raise ValueError('Wiki evidence is present in the export')
    with tempfile.TemporaryDirectory(prefix='wiki-reverse-absent-') as temporary:
        copied = Path(temporary).resolve() / 'catalog.sqlite3'
        if sql._file_digest(catalog, copy_to=copied) != catalog_digest:
            raise ValueError('Current Catalog changed during inspection')
        tables = _inspect_absent(copied)
    absence = {'tables': tables, 'wiki_evidence_present': False, 'wiki_schema_present': False}
    if not stage.exists():
        stage.mkdir(mode=0o700)
        _sync_directory(stage.parent)
    stage_identity = control.identity(stage)
    with control.lock(stage, exclusive=True) as lease:
        lease_identity = os.fstat(lease)
        plan = {'format': FORMAT, 'source_revision': source_revision,
                'inputs': {'current_workspace': {'path': str(current),
                                                 'manifest_sha256': hashlib.sha256(export_bytes).hexdigest(),
                                                 'catalog_sha256': catalog_digest}},
                'output_identity': stage_identity, 'absence': absence}
        result = {'format': FORMAT, 'plan': plan, 'state': STATE,
                  'activation_allowed': False, 'rollback_completed': False,
                  'credential_continuity_verified': False, 'indexes_rebuilt': False,
                  'installation_path_rebound': False}
        if len(control.encoded(result)) > files.MAX_JSON:
            raise ValueError('Attestation budget exceeded')
        marker = stage / 'manifest.json'
        complete = False
        if marker.exists() or marker.is_symlink():
            if _json_file(marker) != result:
                raise ValueError('Attestation changed')
            complete = True
        for entry in stage.iterdir():
            if entry.name in {'.writer-authority.lock', 'manifest.json'}:
                continue
            if re.fullmatch(r'\.manifest\.json\.tmp-[a-f0-9]{16}', entry.name):
                files._check(entry.lstat(), private=True)
                continue
            raise ValueError('Unknown attestation output entry')
        if files._read(current / 'manifest.json', limit=files.MAX_JSON, private=True)[0] != export_bytes:
            raise ValueError('Workspace export changed')
        if files._inventory(current / 'raw', private=True) != export_manifest['plan']['source_inventory']:
            raise ValueError('Export raw content changed')
        if files._inventory(current / 'normalized', private=True) != export_manifest['normalized_inventory']:
            raise ValueError('Export normalized content changed')
        if sql._file_digest(catalog) != catalog_digest:
            raise ValueError('Export Catalog changed')
        current_lock = (stage / '.writer-authority.lock').lstat()
        if (control.identity(stage) != stage_identity
                or (current_lock.st_dev, current_lock.st_ino) != (lease_identity.st_dev, lease_identity.st_ino)):
            raise ValueError('Attestation control identity changed')
        if not complete:
            control._replace(marker, result)
        return {'format': FORMAT, 'state': STATE, 'source_revision': source_revision,
                'plan_sha256': control.digest(plan), 'tables_attested_absent': len(tables),
                'idempotent': complete, 'wiki_domain_absent': True,
                'activation_allowed': False, 'rollback_completed': False,
                'credential_continuity_verified': False, 'indexes_rebuilt': False,
                'installation_path_rebound': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('current-workspace', 'source-revision', 'output'):
        parser.add_argument('--' + name, required=True)
    args = parser.parse_args(argv)
    try:
        result = attest_wiki_absent(args.current_workspace, args.output, source_revision=args.source_revision)
    except Exception:
        print(json.dumps({'format': FORMAT, 'status': 'error', 'error_code': 'wiki_reverse_absent_rejected',
                          'activation_allowed': False}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
