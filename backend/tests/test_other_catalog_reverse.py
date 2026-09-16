import hashlib
import json
import shutil
import sqlite3
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, text

from knowledge_platform.catalog.migrations import migrate_to_latest
from knowledge_platform.catalog.models import (
    KnowledgeAuthoringEvent, KnowledgeAuthoringJob, KnowledgeConnector, KnowledgeCredential,
    KnowledgeCredentialGrant, KnowledgeIngestionEvent, KnowledgeIngestionJob,
    KnowledgeOAuthSession, KnowledgeProcessingJob, KnowledgeSyncRun,
)
from knowledge_platform.distribution import other_catalog_reverse as other
from knowledge_platform.distribution.core_catalog_reverse import build_core_catalog_reverse
from knowledge_platform.distribution.document_migration import prepare_document_migration
from knowledge_platform.distribution.other_catalog_reverse import build_other_catalog_reverse
from knowledge_platform.local.writer_authority import encoded
from test_document_migration import _legacy_catalog

CAPTURED = 'captured_in_frozen_export'
UNCHANGED = 'verified_unchanged'
NOW = datetime(2026, 9, 16, tzinfo=timezone.utc)


def _catalog(path, mutate=None):
    engine = create_engine(f'sqlite:///{path}')
    try:
        with engine.begin() as connection:
            migrate_to_latest(connection)
            if mutate is not None:
                mutate(connection)
    finally:
        engine.dispose()


def _pair(tmp_path, before_mutate=None, after_mutate=None):
    before, after = tmp_path / 'before.sqlite3', tmp_path / 'after.sqlite3'
    _catalog(before, before_mutate)
    shutil.copyfile(before, after)
    if after_mutate is not None:
        engine = create_engine(f'sqlite:///{after}')
        try:
            with engine.begin() as connection:
                after_mutate(connection)
        finally:
            engine.dispose()
    return before, after


def _export(root, catalog):
    export = root / 'export'
    (export / 'raw').mkdir(parents=True)
    export.chmod(0o700); (export / 'raw').chmod(0o700)
    data = catalog.read_bytes()
    copied = export / 'raw' / 'catalog.sqlite3'
    copied.write_bytes(data); copied.chmod(0o600)
    fact = {'sha256': hashlib.sha256(data).hexdigest(), 'size_bytes': len(data)}
    plan = {'format': other.EXPORT_FORMAT,
            'source_inventory': {'files': {'catalog.sqlite3': fact}, 'directories': []}}
    manifest = {'format': other.EXPORT_FORMAT, 'plan': plan,
                'plan_sha256': hashlib.sha256(encoded(plan)).hexdigest(),
                'state': other.EXPORT_STATE, 'activation_allowed': False,
                'rollback_completed': False, 'legacy_schema_converted': False,
                'credential_continuity_verified': False}
    (export / 'manifest.json').write_bytes(encoded(manifest))
    (export / 'manifest.json').chmod(0o600)
    return export


def _run(tmp_path, before, after, export):
    return build_other_catalog_reverse(before, after, export, tmp_path / 'receipt.json')


def _entries(result):
    return {entry['table']: entry for entry in result['tables']}


def add_connector(connection):
    connection.execute(KnowledgeConnector.__table__.insert().values(
        id='connector-1', space_id='space_kb_default', connector_key='local', name='Connector'))


def add_ingestion(status):
    return lambda connection: connection.execute(KnowledgeIngestionJob.__table__.insert().values(
        id='job-1', space_id='space_kb_default', status=status))


def add_processing(status):
    return lambda connection: connection.execute(KnowledgeProcessingJob.__table__.insert().values(
        id='job-1', space_id='space_kb_default', status=status, created_at=NOW, updated_at=NOW))


def add_authoring(status):
    return lambda connection: connection.execute(KnowledgeAuthoringJob.__table__.insert().values(
        id='job-1', dimension_id='dim-1', adapter='local', scope_uri='knowledge://scope',
        status=status, created_at=NOW, updated_at=NOW))


def add_oauth(status):
    return lambda connection: connection.execute(KnowledgeOAuthSession.__table__.insert().values(
        id='session-1', state_hash='hash', credential_id='cred', connector_id='connector',
        principal_id='user', redirect_uri_digest='digest', verifier_credential_ref='vault://v',
        expires_at=NOW, status=status))


def test_all_tables_verified_unchanged(tmp_path):
    before, after = _pair(tmp_path)
    export = _export(tmp_path, after)
    result = _run(tmp_path, before, after, export)
    assert result['state'] == 'verified_other_catalog_disposition' and result['idempotent'] is False
    assert set(_entries(result)) == set(other.NON_CORE)
    assert all(entry['disposition'] == UNCHANGED for entry in result['tables'])
    assert all(entry['rows_before'] == entry['rows_after'] == 0 for entry in result['tables'])
    for flag in other.FLAGS:
        assert result[flag] is False
    receipt = tmp_path / 'receipt.json'
    assert receipt.stat().st_mode & 0o777 == 0o600
    assert json.loads(receipt.read_bytes()) == {
        key: value for key, value in result.items() if key not in ('idempotent', 'receipt_sha256')}
    assert result['receipt_sha256'] == hashlib.sha256(receipt.read_bytes()).hexdigest()
    assert result['frozen_export']['manifest_sha256'] == hashlib.sha256((export / 'manifest.json').read_bytes()).hexdigest()
    assert result['frozen_export']['catalog_sha256'] == hashlib.sha256(after.read_bytes()).hexdigest()
    assert result['schema_versions_identical'] is True


def test_changed_connector_is_captured_in_frozen_export(tmp_path):
    before, after = _pair(tmp_path, after_mutate=add_connector)
    result = _run(tmp_path, before, after, _export(tmp_path, after))
    entry = _entries(result)['knowledge_connectors']
    assert entry['disposition'] == CAPTURED
    assert (entry['rows_before'], entry['rows_after']) == (0, 1)
    assert entry['digest_before'] != entry['digest_after']
    assert all(other_entry['disposition'] == UNCHANGED
               for other_entry in result['tables'] if other_entry['table'] != 'knowledge_connectors')


def test_credentials_and_grants_are_captured_like_other_domains(tmp_path):
    def mutate(connection):
        connection.execute(KnowledgeCredential.__table__.insert().values(
            id='cred-1', owner_principal_id='user-1', provider='feishu', kind='user_oauth',
            external_key='key-1', credential_ref='vault://cred'))
        connection.execute(KnowledgeCredentialGrant.__table__.insert().values(
            id='grant-1', credential_id='cred-1', principal_id='user-1',
            token_credential_ref='vault://token'))
    before, after = _pair(tmp_path, after_mutate=mutate)
    result = _run(tmp_path, before, after, _export(tmp_path, after))
    entries = _entries(result)
    assert entries['knowledge_credentials']['disposition'] == CAPTURED
    assert entries['knowledge_credential_grants']['disposition'] == CAPTURED


def test_sync_run_rows_are_capture_only_not_queue_gated(tmp_path):
    # Design: knowledge_sync_runs is not in the job-queue gate; legacy never had
    # it and a later re-cutover restores it from the frozen export.
    before, after = _pair(tmp_path, after_mutate=lambda connection: connection.execute(
        KnowledgeSyncRun.__table__.insert().values(id='run-1', connector_id='c-1', status='running')))
    result = _run(tmp_path, before, after, _export(tmp_path, after))
    assert _entries(result)['knowledge_sync_runs']['disposition'] == CAPTURED


def test_event_table_delta_is_captured(tmp_path):
    def mutate(connection):
        connection.execute(KnowledgeIngestionEvent.__table__.insert().values(
            id='event-1', job_id='job-external', message='done'))
        connection.execute(KnowledgeAuthoringJob.__table__.insert().values(
            id='aj-1', dimension_id='dim-1', adapter='local', scope_uri='knowledge://scope',
            status='published', created_at=NOW, updated_at=NOW))
        connection.execute(KnowledgeAuthoringEvent.__table__.insert().values(
            id='event-2', job_id='aj-1', message='done', created_at=NOW))
    before, after = _pair(tmp_path, after_mutate=mutate)
    result = _run(tmp_path, before, after, _export(tmp_path, after))
    entries = _entries(result)
    assert entries['knowledge_ingestion_events']['disposition'] == CAPTURED
    assert entries['knowledge_authoring_events']['disposition'] == CAPTURED


@pytest.mark.parametrize('add,status', [
    (add_ingestion, 'queued'), (add_ingestion, 'running'), (add_ingestion, 'mystery'),
    (add_processing, 'queued'), (add_processing, 'staged'), (add_processing, 'running'),
    (add_authoring, 'queued'), (add_authoring, 'waiting_for_publish_confirmation'),
    (add_authoring, 'waiting_for_baseline_change_confirmation'),
    (add_oauth, 'pending'), (add_oauth, 'exchanging'),
])
def test_non_terminal_queue_rows_refuse_the_rollback(tmp_path, add, status):
    before, after = _pair(tmp_path, after_mutate=add(status))
    with pytest.raises(ValueError, match='not drained'):
        _run(tmp_path, before, after, _export(tmp_path, after))
    assert not (tmp_path / 'receipt.json').exists()


@pytest.mark.parametrize('add,status', [
    (add_ingestion, 'succeeded'), (add_ingestion, 'failed'),
    (add_processing, 'succeeded'), (add_processing, 'failed'), (add_processing, 'cancelled'),
    (add_authoring, 'published'), (add_authoring, 'failed'), (add_authoring, 'cancelled'),
    (add_oauth, 'consumed'), (add_oauth, 'superseded'), (add_oauth, 'failed'),
    (add_oauth, 'revoked'), (add_oauth, 'expired'),
])
def test_terminal_queue_rows_are_captured(tmp_path, add, status):
    before, after = _pair(tmp_path, after_mutate=add(status))
    result = _run(tmp_path, before, after, _export(tmp_path, after))
    changed = [entry for entry in result['tables'] if entry['disposition'] == CAPTURED]
    assert len(changed) == 1 and changed[0]['rows_after'] == 1


def test_nonterminal_job_in_unchanged_table_still_refuses(tmp_path):
    # The drain gate judges the after image, not the delta: a job left queued at
    # freeze time refuses even when the job table never changed in the window.
    before, after = _pair(tmp_path, before_mutate=add_ingestion('queued'))
    with pytest.raises(ValueError, match='not drained'):
        _run(tmp_path, before, after, _export(tmp_path, after))


def test_unknown_table_rejects_even_when_unchanged(tmp_path):
    def mutate(connection):
        connection.execute(text('CREATE TABLE mystery (id TEXT PRIMARY KEY)'))
    before, after = _pair(tmp_path, before_mutate=mutate)
    with pytest.raises(ValueError, match='Unknown target domain table'):
        _run(tmp_path, before, after, _export(tmp_path, after))


@pytest.mark.parametrize('drift', [
    'ALTER TABLE knowledge_connectors ADD COLUMN extra TEXT',
    'CREATE INDEX ix_extra ON knowledge_connectors (name)',
    'CREATE TABLE mystery (id TEXT PRIMARY KEY)',
])
def test_schema_drift_between_snapshots_rejects(tmp_path, drift):
    before, after = _pair(tmp_path)
    with sqlite3.connect(after) as db:
        db.execute(drift)
    with pytest.raises(ValueError, match='Target schema changed'):
        _run(tmp_path, before, after, _export(tmp_path, after))


def test_schema_versioning_drift_rejects(tmp_path):
    def mutate(connection):
        connection.execute(text(
            "INSERT INTO knowledge_catalog_schema_versions (version, applied_at) VALUES (13, 'now')"))
    before, after = _pair(tmp_path, after_mutate=mutate)
    with pytest.raises(ValueError, match='schema versioning'):
        _run(tmp_path, before, after, _export(tmp_path, after))


@pytest.mark.parametrize('mode', ['manifest_missing', 'state_copying', 'fact_mismatch',
                                  'raw_changed', 'binds_before', 'wal_sidecar'])
def test_missing_or_mismatched_frozen_export_evidence_rejects(tmp_path, mode):
    before, after = _pair(tmp_path, after_mutate=add_connector)
    export = _export(tmp_path, after)
    if mode == 'manifest_missing':
        (export / 'manifest.json').unlink()
    elif mode == 'state_copying':
        manifest = json.loads((export / 'manifest.json').read_bytes())
        manifest['state'] = 'copying'
        (export / 'manifest.json').write_bytes(encoded(manifest))
    elif mode == 'fact_mismatch':
        manifest = json.loads((export / 'manifest.json').read_bytes())
        manifest['plan']['source_inventory']['files']['catalog.sqlite3']['sha256'] = '0' * 64
        (export / 'manifest.json').write_bytes(encoded(manifest))
    elif mode == 'raw_changed':
        (export / 'raw' / 'catalog.sqlite3').write_bytes(b'not a catalog')
    elif mode == 'binds_before':
        export = _export(tmp_path / 'second', before)
    elif mode == 'wal_sidecar':
        sidecar = export / 'raw' / 'catalog.sqlite3-wal'
        sidecar.write_bytes(b'wal'); sidecar.chmod(0o600)
    with pytest.raises((ValueError, OSError)):
        _run(tmp_path, before, after, export)
    assert not (tmp_path / 'receipt.json').exists()


def test_export_manifest_must_be_canonical(tmp_path):
    before, after = _pair(tmp_path)
    export = _export(tmp_path, after)
    manifest = json.loads((export / 'manifest.json').read_bytes())
    (export / 'manifest.json').write_bytes(json.dumps(manifest, indent=2).encode())
    with pytest.raises(ValueError, match='canonical'):
        _run(tmp_path, before, after, export)


def test_exact_retry_is_byte_identical_and_tamper_refuses(tmp_path):
    before, after = _pair(tmp_path, after_mutate=add_connector)
    export = _export(tmp_path, after)
    first = _run(tmp_path, before, after, export)
    data = (tmp_path / 'receipt.json').read_bytes()
    second = _run(tmp_path, before, after, export)
    assert second == {**first, 'idempotent': True}
    assert (tmp_path / 'receipt.json').read_bytes() == data
    (tmp_path / 'receipt.json').write_bytes(b'{}\n')
    (tmp_path / 'receipt.json').chmod(0o600)
    with pytest.raises(ValueError, match='disagrees'):
        _run(tmp_path, before, after, export)


def test_interrupted_part_file_is_recovered(tmp_path):
    before, after = _pair(tmp_path)
    export = _export(tmp_path, after)
    part = tmp_path / 'receipt.json.part'
    part.write_bytes(b'interrupted'); part.chmod(0o600)
    result = _run(tmp_path, before, after, export)
    assert result['idempotent'] is False and not part.exists()


def test_input_snapshot_mutation_during_run_rejects(tmp_path):
    before, after = _pair(tmp_path, after_mutate=add_connector)
    export = _export(tmp_path, after)
    def mutate():
        after.write_bytes(b'mutated')
    with pytest.raises(ValueError, match='Input snapshot changed'):
        build_other_catalog_reverse(before, after, export, tmp_path / 'receipt.json',
                                    _after_snapshot=mutate)
    assert not (tmp_path / 'receipt.json').exists()


def test_snapshot_mutation_between_runs_refuses_existing_receipt(tmp_path):
    before, after = _pair(tmp_path)
    export = _export(tmp_path, after)
    assert _run(tmp_path, before, after, export)['idempotent'] is False
    engine = create_engine(f'sqlite:///{before}')
    with engine.begin() as connection:
        add_connector(connection)
    engine.dispose()
    with pytest.raises(ValueError, match='disagrees'):
        _run(tmp_path, before, after, export)


def _core_fixture(root):
    root.mkdir()
    source = root / 'source.sqlite3'
    body = b'unchanged content'
    _legacy_catalog(source, content_digest=hashlib.sha256(body).hexdigest())
    files = root / 'files'; files.mkdir(); (files / 'body.md').write_bytes(body)
    prepare_document_migration(source, files, {'doc-1': 'body.md'}, root / 'package',
                               source_revision='legacy-1')
    before = root / 'package' / 'catalog.sqlite3'
    after = root / 'after.sqlite3'
    shutil.copyfile(before, after)
    return source, before, after


def _change_after(after):
    engine = create_engine(f'sqlite:///{after}')
    try:
        with engine.begin() as connection:
            add_connector(connection)
    finally:
        engine.dispose()


def test_core_reverse_still_refuses_changed_noncore_without_receipt(tmp_path):
    source, before, after = _core_fixture(tmp_path / 'core')
    _change_after(after)
    with pytest.raises(ValueError, match='Unmapped'):
        build_core_catalog_reverse(source, before, after, tmp_path / 'out.sqlite3',
                                   source_revision='legacy-1')
    assert not (tmp_path / 'out.sqlite3').exists()


def test_core_reverse_proceeds_with_covering_disposition_receipt(tmp_path):
    source, before, after = _core_fixture(tmp_path / 'core')
    _change_after(after)
    receipt = tmp_path / 'receipt.json'
    build_other_catalog_reverse(before, after, _export(tmp_path, after), receipt)
    result = build_core_catalog_reverse(source, before, after, tmp_path / 'out.sqlite3',
                                        source_revision='legacy-1', other_catalog_disposition=receipt)
    assert result['state'] == 'verified_inactive_core_metadata'
    base_source, base_before, base_after = _core_fixture(tmp_path / 'baseline')
    baseline = build_core_catalog_reverse(base_source, base_before, base_after,
                                          tmp_path / 'baseline-out.sqlite3', source_revision='legacy-1')
    assert result['changes'] == baseline['changes']
    assert (tmp_path / 'out.sqlite3').read_bytes() == (tmp_path / 'baseline-out.sqlite3').read_bytes()


def test_core_reverse_refuses_receipt_that_misses_a_changed_table(tmp_path):
    source, before, after = _core_fixture(tmp_path / 'core')
    _change_after(after)
    receipt = tmp_path / 'receipt.json'
    build_other_catalog_reverse(before, after, _export(tmp_path, after), receipt)
    engine = create_engine(f'sqlite:///{after}')
    with engine.begin() as connection:
        connection.execute(KnowledgeCredential.__table__.insert().values(
            id='cred-1', owner_principal_id='user-1', provider='feishu', kind='user_oauth',
            external_key='key-1', credential_ref='vault://cred'))
    engine.dispose()
    with pytest.raises(ValueError, match='Unmapped'):
        build_core_catalog_reverse(source, before, after, tmp_path / 'out.sqlite3',
                                   source_revision='legacy-1', other_catalog_disposition=receipt)
    assert not (tmp_path / 'out.sqlite3').exists()


@pytest.mark.parametrize('tamper', ['identity', 'disposition', 'flags', 'truncate'])
def test_core_reverse_refuses_tampered_disposition_receipt(tmp_path, tamper):
    source, before, after = _core_fixture(tmp_path / 'core')
    _change_after(after)
    receipt = tmp_path / 'receipt.json'
    build_other_catalog_reverse(before, after, _export(tmp_path, after), receipt)
    if tamper == 'truncate':
        receipt.write_bytes(b'{}\n')
    else:
        payload = json.loads(receipt.read_bytes())
        if tamper == 'identity':
            payload['target_after_sha256'] = '0' * 64
        elif tamper == 'disposition':
            payload['tables'] = [{**entry, 'disposition': UNCHANGED} for entry in payload['tables']]
        elif tamper == 'flags':
            payload['indexes_rebuilt'] = True
        receipt.write_bytes(encoded(payload))
    receipt.chmod(0o600)
    with pytest.raises(ValueError):
        build_core_catalog_reverse(source, before, after, tmp_path / 'out.sqlite3',
                                   source_revision='legacy-1', other_catalog_disposition=receipt)
    assert not (tmp_path / 'out.sqlite3').exists()


def test_core_reverse_accepts_unchanged_disposition_receipt(tmp_path):
    source, before, after = _core_fixture(tmp_path / 'core')
    receipt = tmp_path / 'receipt.json'
    build_other_catalog_reverse(before, after, _export(tmp_path, after), receipt)
    result = build_core_catalog_reverse(source, before, after, tmp_path / 'out.sqlite3',
                                        source_revision='legacy-1', other_catalog_disposition=receipt)
    assert result['state'] == 'verified_inactive_core_metadata'


def test_real_frozen_workspace_export_binds_disposition(tmp_path):
    from knowledge_platform.local.frozen_export import export_frozen_workspace
    from knowledge_platform.local.writer_authority import enroll, suspend
    from knowledge_platform.local.workspace import open_persistent_workspace
    source = tmp_path / 'source.sqlite3'
    engine = create_engine(f'sqlite:///{source}')
    with engine.begin() as connection:
        migrate_to_latest(connection)
        connection.execute(text(
            "INSERT INTO knowledge_spaces (id, name, description, permissions_json, created_at, updated_at)"
            " VALUES ('space_kb_default', 'Workspace fixture', 'local', '{}', 'now', 'now')"))
        connection.execute(text(
            "INSERT INTO knowledge_datasets (id, space_id, name, version, kind, description, asset_ids,"
            " semantic_asset_ids, capabilities, freshness, permissions_json, manifest_digest, created_at, updated_at)"
            " VALUES ('dataset_kb_default', 'space_kb_default', 'Workspace fixture', '1', 'wiki', 'local',"
            " '[]', '[]', '[]', '{}', '{}', '', 'now', 'now')"))
    engine.dispose()
    _change_after(source)
    wiki = tmp_path / 'wiki'; wiki.mkdir(mode=0o700)
    (wiki / 'guide.md').write_text('# Guide\n', encoding='utf-8')
    state = tmp_path / 'state'
    with open_persistent_workspace(state, catalog=source, wiki_root=wiki):
        pass
    enroll(state, tmp_path / 'authority', 'enrollment-1')
    suspend(state, 'freeze-1')
    export = tmp_path / 'export'
    assert export_frozen_workspace(state, export, 'freeze-1')['state'] == 'verified_frozen_export'
    assert not (state / 'catalog.sqlite3-wal').exists()
    after = tmp_path / 'after.sqlite3'
    shutil.copyfile(state / 'catalog.sqlite3', after)
    before = tmp_path / 'before.sqlite3'
    shutil.copyfile(after, before)
    with sqlite3.connect(before) as db:
        db.execute('DELETE FROM knowledge_connectors')
    result = build_other_catalog_reverse(before, after, export, tmp_path / 'receipt.json')
    assert _entries(result)['knowledge_connectors']['disposition'] == CAPTURED
    assert result['frozen_export'] == {
        'manifest_sha256': hashlib.sha256((export / 'manifest.json').read_bytes()).hexdigest(),
        'catalog_sha256': hashlib.sha256(after.read_bytes()).hexdigest()}


def test_cli_success_and_failure(tmp_path, capsys):
    before, after = _pair(tmp_path, after_mutate=add_connector)
    export = _export(tmp_path, after)
    args = ['--target-before', str(before), '--target-after', str(after),
            '--frozen-export', str(export), '--output', str(tmp_path / 'receipt.json')]
    assert other.main(args) == 0
    assert json.loads(capsys.readouterr().out)['format'] == other.FORMAT
    args[-1] = str(tmp_path / 'second.json')
    args[args.index('--frozen-export') + 1] = str(tmp_path / 'missing-export')
    assert other.main(args) == 1
    failure = json.loads(capsys.readouterr().out)
    assert failure['error_code'] == 'other_catalog_reverse_rejected'
    assert failure['activation_allowed'] is False and failure['rollback_completed'] is False
    assert str(tmp_path) not in json.dumps(failure)
    assert not (tmp_path / 'second.json').exists()
