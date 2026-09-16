import hashlib
import json
import sqlite3

import pytest

from knowledge_platform.distribution import wiki_reverse_absent as absent
from knowledge_platform.distribution.wiki_reverse import _STATE_TABLES
from knowledge_platform.distribution.wiki_reverse_absent import FORMAT, STATE, attest_wiki_absent, main
from knowledge_platform.local.frozen_export import export_frozen_workspace
from knowledge_platform.local.workspace import open_persistent_workspace
from knowledge_platform.local.writer_authority import digest, encoded, enroll, identity, suspend
from test_core_catalog_reverse import fixture as core_fixture
from test_wiki_reverse import _rebind_export

_DOMAIN_INSERTS = {
    'knowledge_spaces':
        "INSERT INTO knowledge_spaces (id,name,description,permissions_json,created_at,updated_at) "
        "VALUES ('space_wiki_refuse','Migrated Wiki','{}','{}','now','now')",
    'knowledge_datasets':
        "INSERT INTO knowledge_datasets (id,space_id,name,version,kind,description,asset_ids,semantic_asset_ids,"
        "capabilities,freshness,permissions_json,manifest_digest,created_at,updated_at) "
        "VALUES ('collection_wiki_refuse','space_wiki_refuse','Migrated Wiki','1','wiki','','[]','[]',"
        "'[\"wiki_query\"]','{}','{}','','now','now')",
    'knowledge_assets':
        "INSERT INTO knowledge_assets (id,space_id,kind,title,description,mime_type,source_type,source_uri,revision,"
        "content_digest,permissions_json,metadata_json,created_at,updated_at) "
        "VALUES ('asset_wiki_refuse','space_wiki_refuse','wiki_page','Concept','','text/markdown','local_published_wiki',"
        "'knowledge://spaces/space_wiki_refuse/assets/asset_wiki_refuse','sha256:" + '0' * 64 + "',"
        "'sha256:" + '0' * 64 + "','{}','{}','now','now')",
}


def _export(root, *, wiki_members=()):
    """Frozen export of a suspended workspace whose Catalog holds no Wiki domain.

    Built the same way as the Catalog-chain workspaces (test_core_catalog_reverse
    fixture plus open_persistent_workspace), enrolled and suspended; every domain
    row is then deleted, leaving the empty-Wiki-domain Home the rehearsal attests.
    """
    root.mkdir()
    core_fixture(root, real=True)
    state = root / 'state'
    with open_persistent_workspace(state, document_migration=root / 'package'):
        pass
    enroll(state, root / 'authority', 'enrollment-1')
    suspend(state, 'freeze-1')
    for name in wiki_members:
        member = state / name
        if name == 'wiki-evidence':
            member.mkdir(mode=0o700)
            (member / 'marker').write_bytes(b'x')
            (member / 'marker').chmod(0o600)
        else:
            member.write_bytes(b'{}')
            member.chmod(0o600)
    with sqlite3.connect(state / 'catalog.sqlite3') as db:
        for table in ('knowledge_collection_bindings', 'knowledge_datasets', 'knowledge_assets', 'knowledge_spaces'):
            db.execute(f'DELETE FROM {table}')
    export = root / 'export'
    assert export_frozen_workspace(state, export, 'freeze-1')['state'] == 'verified_frozen_export'
    return export


def _attest(export, out, revision='legacy-1'):
    return attest_wiki_absent(export, out, source_revision=revision)


def test_inspected_tables_mirror_the_active_path():
    assert absent.INSPECTED_TABLES == ('knowledge_spaces', 'knowledge_datasets', 'knowledge_assets') + _STATE_TABLES


def test_absent_attestation_commits_empty_domain_and_is_idempotent(tmp_path):
    export = _export(tmp_path / 'wiki-less')
    out = tmp_path / 'attestation'
    first = _attest(export, out)
    assert first['format'] == FORMAT and first['state'] == 'verified_absent_wiki'
    assert first['source_revision'] == 'legacy-1' and first['tables_attested_absent'] == 9
    assert first['idempotent'] is False and first['wiki_domain_absent'] is True
    for flag in ('activation_allowed', 'rollback_completed', 'credential_continuity_verified',
                 'indexes_rebuilt', 'installation_path_rebound'):
        assert first[flag] is False
    marker = out / 'manifest.json'
    data = marker.read_bytes()
    assert marker.stat().st_mode & 0o777 == 0o600
    assert data == encoded(json.loads(data))
    manifest = json.loads(data)
    assert set(manifest) == {'format', 'plan', 'state', 'activation_allowed', 'rollback_completed',
                             'credential_continuity_verified', 'indexes_rebuilt', 'installation_path_rebound'}
    assert manifest['format'] == FORMAT and manifest['state'] == STATE
    plan = manifest['plan']
    assert set(plan) == {'format', 'source_revision', 'inputs', 'output_identity', 'absence'}
    assert plan['format'] == FORMAT and plan['source_revision'] == 'legacy-1'
    assert plan['output_identity'] == identity(out)
    assert set(plan['inputs']) == {'current_workspace'}
    assert plan['inputs']['current_workspace'] == {
        'path': str(export),
        'manifest_sha256': hashlib.sha256((export / 'manifest.json').read_bytes()).hexdigest(),
        'catalog_sha256': hashlib.sha256((export / 'normalized/catalog/catalog.sqlite3').read_bytes()).hexdigest()}
    expected = [{'table': name, 'present': name in absent.INSPECTED_TABLES[:3], 'rows': 0}
                for name in absent.INSPECTED_TABLES]
    assert plan['absence'] == {'tables': expected, 'wiki_evidence_present': False, 'wiki_schema_present': False}
    assert first['plan_sha256'] == digest(plan)
    second = _attest(export, out)
    assert second == {**first, 'idempotent': True}
    assert marker.read_bytes() == data


def test_empty_wiki_state_tables_still_attest_absent(tmp_path):
    export = _export(tmp_path / 'wiki-less')
    with sqlite3.connect(export / 'normalized/catalog/catalog.sqlite3') as db:
        for table in _STATE_TABLES:
            db.execute(f'CREATE TABLE {table} (space_id TEXT NOT NULL)')
    _rebind_export(export)
    out = tmp_path / 'attestation'
    assert _attest(export, out)['state'] == 'verified_absent_wiki'
    manifest = json.loads((out / 'manifest.json').read_text())
    assert [entry['present'] for entry in manifest['plan']['absence']['tables']] == [True] * 9


@pytest.mark.parametrize('table', ['knowledge_spaces', 'knowledge_datasets', 'knowledge_assets'])
def test_domain_row_refuses(tmp_path, table):
    export = _export(tmp_path / 'wiki-less')
    with sqlite3.connect(export / 'normalized/catalog/catalog.sqlite3') as db:
        db.execute(_DOMAIN_INSERTS[table])
    _rebind_export(export)
    with pytest.raises(ValueError, match='Wiki domain is present'):
        _attest(export, tmp_path / 'attestation')
    assert not (tmp_path / 'attestation').exists()


@pytest.mark.parametrize('table', list(_STATE_TABLES))
def test_wiki_state_row_refuses(tmp_path, table):
    export = _export(tmp_path / 'wiki-less')
    with sqlite3.connect(export / 'normalized/catalog/catalog.sqlite3') as db:
        db.execute(f'CREATE TABLE {table} (space_id TEXT NOT NULL)')
        db.execute(f"INSERT INTO {table} VALUES ('space_wiki_refuse')")
    _rebind_export(export)
    with pytest.raises(ValueError, match='Wiki domain is present'):
        _attest(export, tmp_path / 'attestation')
    assert not (tmp_path / 'attestation').exists()


def test_unknown_table_refuses(tmp_path):
    export = _export(tmp_path / 'wiki-less')
    with sqlite3.connect(export / 'normalized/catalog/catalog.sqlite3') as db:
        db.execute('CREATE TABLE knowledge_wiki_shadow (space_id TEXT NOT NULL)')
    _rebind_export(export)
    with pytest.raises(ValueError, match='Unknown Catalog table'):
        _attest(export, tmp_path / 'attestation')


def test_missing_domain_table_refuses(tmp_path):
    export = _export(tmp_path / 'wiki-less')
    with sqlite3.connect(export / 'normalized/catalog/catalog.sqlite3') as db:
        db.execute('DROP TABLE knowledge_spaces')
    _rebind_export(export)
    with pytest.raises(ValueError, match='schema is incomplete'):
        _attest(export, tmp_path / 'attestation')


def test_export_catalog_tamper_refuses(tmp_path):
    export = _export(tmp_path / 'wiki-less')
    with sqlite3.connect(export / 'normalized/catalog/catalog.sqlite3') as db:
        db.execute(_DOMAIN_INSERTS['knowledge_spaces'])
    with pytest.raises(ValueError):
        _attest(export, tmp_path / 'attestation')


@pytest.mark.parametrize('member', ['wiki-evidence', 'wiki-schema.json'])
def test_export_wiki_member_refuses(tmp_path, member):
    export = _export(tmp_path / 'wiki-less', wiki_members=(member,))
    with pytest.raises(ValueError, match='Wiki evidence is present'):
        _attest(export, tmp_path / 'attestation')


@pytest.mark.parametrize('revision', ['', 'has space', 'x' * 81, None])
def test_invalid_source_revision_refuses(tmp_path, revision):
    export = _export(tmp_path / 'wiki-less')
    with pytest.raises(ValueError, match='Invalid source revision'):
        _attest(export, tmp_path / 'attestation', revision=revision)


def test_output_overlapping_input_refuses(tmp_path):
    export = _export(tmp_path / 'wiki-less')
    with pytest.raises(ValueError):
        _attest(export, export / 'attestation')


def test_unowned_output_refuses(tmp_path):
    export = _export(tmp_path / 'wiki-less')
    out = tmp_path / 'attestation'
    out.mkdir(mode=0o700)
    (out / 'foreign').write_bytes(b'x')
    with pytest.raises(ValueError, match='Unknown attestation output entry'):
        _attest(export, out)


def test_changed_attestation_refuses_without_repair(tmp_path):
    export = _export(tmp_path / 'wiki-less')
    out = tmp_path / 'attestation'
    _attest(export, out)
    marker = out / 'manifest.json'
    data = marker.read_bytes()
    with pytest.raises(ValueError, match='Attestation changed'):
        _attest(export, out, revision='legacy-2')
    payload = json.loads(data)
    payload['activation_allowed'] = True
    marker.write_bytes(encoded(payload))
    marker.chmod(0o600)
    with pytest.raises(ValueError, match='Attestation changed'):
        _attest(export, out)
    assert json.loads(marker.read_bytes())['activation_allowed'] is True


def test_cli_success_and_rejection(tmp_path, capsys):
    export = _export(tmp_path / 'wiki-less')
    out = tmp_path / 'attestation'
    assert main(['--current-workspace', str(export), '--source-revision', 'legacy-1',
                 '--output', str(out)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['format'] == FORMAT and result['state'] == 'verified_absent_wiki'
    assert main(['--current-workspace', str(tmp_path / 'missing'), '--source-revision', 'legacy-1',
                 '--output', str(tmp_path / 'other')]) == 1
    error = json.loads(capsys.readouterr().out)
    assert error['error_code'] == 'wiki_reverse_absent_rejected' and error['activation_allowed'] is False
    assert str(tmp_path) not in json.dumps(error)
    assert not (tmp_path / 'other').exists()
