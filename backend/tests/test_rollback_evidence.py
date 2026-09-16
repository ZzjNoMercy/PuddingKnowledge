import hashlib
import json
import shutil
import sqlite3

import pytest

from knowledge_platform.distribution import rollback_evidence as evidence
from knowledge_platform.distribution.document_reverse import prepare_document_reverse
from knowledge_platform.distribution.other_catalog_reverse import build_other_catalog_reverse
from knowledge_platform.distribution.wiki_reverse import prepare_wiki_reverse
from knowledge_platform.local.frozen_export import export_frozen_workspace
from knowledge_platform.local.workspace import open_persistent_workspace
from knowledge_platform.local.writer_authority import encoded, enroll, suspend
from test_core_catalog_reverse import fixture as core_fixture
from test_wiki_reverse import _pipeline

ZERO = '0' * 64


def _document_lineage(root):
    """Real Catalog chain: migration package workspace, suspended and exported."""
    root.mkdir()
    source, _package_before, _unused_after, _ = core_fixture(root, real=True)
    package = root / 'package'
    state = root / 'state'
    with open_persistent_workspace(state, document_migration=package):
        pass
    before = root / 'target-before.sqlite3'
    shutil.copyfile(state / 'catalog.sqlite3', before)
    before.chmod(0o600)
    # A title-only delta keeps the migrated workspace facts consistent; the
    # document body stays the migration original, so the binding below is exact.
    with sqlite3.connect(state / 'catalog.sqlite3') as db:
        asset_id, digest = db.execute(
            "SELECT id, content_digest FROM knowledge_assets WHERE kind='document'").fetchone()
        db.execute('UPDATE knowledge_assets SET title=?', ('Current title',))
    bodies = root / 'current-bodies'
    bodies.mkdir(mode=0o700)
    body = b'unchanged content'
    (bodies / 'body.md').write_bytes(body)
    assert digest == 'sha256:' + hashlib.sha256(body).hexdigest()
    bindings = {asset_id: 'body.md'}
    enroll(state, root / 'authority', 'enrollment-1')
    suspend(state, 'freeze-1')
    export = root / 'export'
    assert export_frozen_workspace(state, export, 'freeze-1')['state'] == 'verified_frozen_export'
    after = root / 'target-after.sqlite3'
    shutil.copyfile(state / 'catalog.sqlite3', after)
    after.chmod(0o600)
    disposition = root / 'disposition.json'
    assert build_other_catalog_reverse(before, after, export, disposition)['state'] == \
        'verified_other_catalog_disposition'
    reverse = root / 'reverse'
    assert prepare_document_reverse(source, before, after, bodies, bindings, reverse,
                                    source_revision='legacy-1')['state'] == 'verified_inactive_documents'
    return {'source': source, 'before': before, 'after': after, 'export': export,
            'disposition': disposition, 'reverse': reverse}


def _wiki_lineage(root):
    """Real Wiki chain: archived brain workspace, suspended, exported, reversed."""
    root.mkdir()
    brain, candidate, export, _state, _fixture = _pipeline(root)
    reverse = root / 'reverse'
    assert prepare_wiki_reverse(brain, candidate, export, reverse)['state'] == 'verified_inactive_wiki'
    return {'wiki_reverse': reverse}


def _chains(tmp_path):
    chains = _document_lineage(tmp_path / 'documents')
    chains.update(_wiki_lineage(tmp_path / 'wiki-chain'))
    return chains


def _artifacts(chains):
    return {'frozen_export': chains['export'] / 'manifest.json',
            'other_catalog_disposition': chains['disposition'],
            'document_reverse': chains['reverse'] / 'manifest.json',
            'wiki_reverse': chains['wiki_reverse'] / 'manifest.json'}


def _assemble(chains, output):
    artifacts = _artifacts(chains)
    return evidence.assemble_rollback_evidence(
        artifacts['frozen_export'], artifacts['other_catalog_disposition'],
        artifacts['document_reverse'], artifacts['wiki_reverse'], output)


def _rewrite(path, payload):
    path.write_bytes(encoded(payload))
    path.chmod(0o600)


def _tamper(path, mutate):
    payload = json.loads(path.read_bytes())
    mutate(payload)
    _rewrite(path, payload)


def test_happy_path_assembles_deterministic_evidence(tmp_path):
    chains = _chains(tmp_path)
    output = tmp_path / 'evidence.json'
    result = _assemble(chains, output)
    assert result['format'] == evidence.FORMAT and result['state'] == 'verified_rollback_evidence'
    assert result['idempotent'] is False
    assert result['operation_id'] == 'freeze-1' and result['source_revision'] == 'legacy-1'
    data = output.read_bytes()
    assert output.stat().st_mode & 0o777 == 0o600
    assert data == encoded(json.loads(data))
    assert json.loads(data) == {key: value for key, value in result.items()
                                if key not in ('idempotent', 'evidence_sha256')}
    assert result['evidence_sha256'] == hashlib.sha256(data).hexdigest()
    assert str(tmp_path).encode() not in data
    artifacts = _artifacts(chains)
    expected = [('frozen_export', 'verified_frozen_export'),
                ('other_catalog_disposition', 'verified_other_catalog_disposition'),
                ('document_reverse', 'verified_inactive_documents'),
                ('wiki_reverse', 'verified_inactive_wiki')]
    assert [(entry['role'], entry['state']) for entry in result['artifacts']] == expected
    for entry, (role, _state) in zip(result['artifacts'], expected):
        assert entry['sha256'] == hashlib.sha256(artifacts[role].read_bytes()).hexdigest()
    linkages = result['linkages']
    assert linkages['frozen_export_manifest_sha256'] == result['artifacts'][0]['sha256']
    assert linkages['frozen_export_catalog_sha256'] == hashlib.sha256(chains['after'].read_bytes()).hexdigest()
    assert linkages['target_after_sha256'] == linkages['frozen_export_catalog_sha256']
    assert linkages['target_before_sha256'] == hashlib.sha256(chains['before'].read_bytes()).hexdigest()
    normalized = chains['export'] / 'normalized/catalog/catalog.sqlite3'
    assert linkages['frozen_export_normalized_catalog_sha256'] == \
        hashlib.sha256(normalized.read_bytes()).hexdigest()
    candidate = chains['reverse'] / 'catalog.sqlite3'
    assert linkages['document_candidate_catalog_sha256'] == hashlib.sha256(candidate.read_bytes()).hexdigest()
    assert set(result['artifact_digests']) == {
        'frozen_export_raw_inventory_sha256', 'frozen_export_normalized_inventory_sha256',
        'document_bodies_inventory_sha256', 'wiki_brain_inventory_sha256'}
    assert all(len(value) == 64 for value in result['artifact_digests'].values())
    for flag in evidence.FLAGS:
        assert result[flag] is False


def test_exact_retry_is_byte_identical(tmp_path):
    chains = _chains(tmp_path)
    output = tmp_path / 'evidence.json'
    first = _assemble(chains, output)
    data = output.read_bytes()
    second = _assemble(chains, output)
    assert second == {**first, 'idempotent': True}
    assert output.read_bytes() == data


def test_disagreeing_existing_evidence_refuses(tmp_path):
    chains = _chains(tmp_path)
    output = tmp_path / 'evidence.json'
    output.write_bytes(b'{}\n')
    output.chmod(0o600)
    with pytest.raises(ValueError, match='disagrees'):
        _assemble(chains, output)
    assert output.read_bytes() == b'{}\n'


def test_interrupted_part_file_is_recovered(tmp_path):
    chains = _chains(tmp_path)
    output = tmp_path / 'evidence.json'
    part = tmp_path / 'evidence.json.part'
    part.write_bytes(b'interrupted')
    part.chmod(0o600)
    assert _assemble(chains, output)['idempotent'] is False
    assert not part.exists()


@pytest.mark.parametrize('role', ['frozen_export', 'other_catalog_disposition',
                                  'document_reverse', 'wiki_reverse'])
@pytest.mark.parametrize('tamper', ['format', 'state', 'flag', 'noncanonical'])
def test_artifact_envelope_tamper_refuses(tmp_path, role, tamper):
    chains = _chains(tmp_path)
    path = _artifacts(chains)[role]
    if tamper == 'noncanonical':
        path.write_bytes(json.dumps(json.loads(path.read_bytes()), indent=2).encode())
        path.chmod(0o600)
    elif tamper == 'format':
        _tamper(path, lambda payload: payload.update(format='puddingknowledge-forged/v9'))
    elif tamper == 'state':
        _tamper(path, lambda payload: payload.update(state='copying'))
    else:
        flag = {'frozen_export': 'activation_allowed',
                'other_catalog_disposition': 'indexes_rebuilt',
                'document_reverse': 'rollback_completed',
                'wiki_reverse': 'installation_path_rebound'}[role]
        _tamper(path, lambda payload: payload.update(**{flag: True}))
    with pytest.raises(ValueError):
        _assemble(chains, tmp_path / 'evidence.json')
    assert not (tmp_path / 'evidence.json').exists()


def _linkage_tamper(chains, kind):
    artifacts = _artifacts(chains)
    if kind == 'disposition_manifest':
        _tamper(artifacts['other_catalog_disposition'],
                lambda payload: payload['frozen_export'].update(manifest_sha256=ZERO))
    elif kind == 'disposition_catalog':
        _tamper(artifacts['other_catalog_disposition'],
                lambda payload: payload['frozen_export'].update(catalog_sha256=ZERO))
    elif kind == 'disposition_target_after':
        _tamper(artifacts['other_catalog_disposition'],
                lambda payload: payload.update(target_after_sha256=ZERO))
    elif kind in ('document_target_before', 'document_target_after'):
        index = 1 if kind == 'document_target_before' else 2

        def mutate(payload):
            payload['plan']['inputs'][index]['sha256'] = ZERO
            payload['core_receipt']['input_sha256'][index] = ZERO
        _tamper(artifacts['document_reverse'], mutate)
    elif kind == 'core_output':
        _tamper(artifacts['document_reverse'],
                lambda payload: payload['core_receipt'].update(output_sha256=ZERO))
    elif kind == 'core_source_revision':
        _tamper(artifacts['document_reverse'],
                lambda payload: payload['core_receipt'].update(source_revision='legacy-other'))
    elif kind == 'wiki_source_revision':
        _tamper(artifacts['wiki_reverse'],
                lambda payload: payload['plan'].update(source_revision='legacy-other'))
    elif kind == 'export_catalog_fact':
        def mutate(payload):
            payload['plan']['source_inventory']['files']['catalog.sqlite3']['sha256'] = ZERO
            payload['plan_sha256'] = hashlib.sha256(encoded(payload['plan'])).hexdigest()
        _tamper(artifacts['frozen_export'], mutate)
    else:
        raise AssertionError(kind)


@pytest.mark.parametrize('kind', ['disposition_manifest', 'disposition_catalog',
                                  'disposition_target_after', 'document_target_before',
                                  'document_target_after', 'core_output',
                                  'core_source_revision', 'wiki_source_revision',
                                  'export_catalog_fact'])
def test_broken_cross_linkage_refuses(tmp_path, kind):
    chains = _chains(tmp_path)
    _linkage_tamper(chains, kind)
    with pytest.raises(ValueError):
        _assemble(chains, tmp_path / 'evidence.json')
    assert not (tmp_path / 'evidence.json').exists()


def _damage(chains, kind):
    candidate = chains['reverse'] / 'catalog.sqlite3'
    body = chains['reverse'] / 'bodies/body.md'
    export_raw = chains['export'] / 'raw/catalog.sqlite3'
    export_normalized = chains['export'] / 'normalized/catalog/catalog.sqlite3'
    brain_page = chains['wiki_reverse'] / 'brain/wiki/log.md'
    if kind == 'candidate_catalog_missing':
        candidate.unlink()
    elif kind == 'candidate_catalog_tampered':
        candidate.write_bytes(b'changed')
        candidate.chmod(0o600)
    elif kind == 'body_missing':
        body.unlink()
    elif kind == 'body_tampered':
        body.write_bytes(b'changed')
        body.chmod(0o600)
    elif kind == 'export_raw_catalog_tampered':
        export_raw.write_bytes(b'changed')
        export_raw.chmod(0o600)
    elif kind == 'export_normalized_missing':
        export_normalized.unlink()
    elif kind == 'wiki_brain_tampered':
        brain_page.write_bytes(b'changed')
        brain_page.chmod(0o600)
    else:
        raise AssertionError(kind)


@pytest.mark.parametrize('kind', ['candidate_catalog_missing', 'candidate_catalog_tampered',
                                  'body_missing', 'body_tampered', 'export_raw_catalog_tampered',
                                  'export_normalized_missing', 'wiki_brain_tampered'])
def test_missing_or_divergent_committed_output_refuses(tmp_path, kind):
    chains = _chains(tmp_path)
    _damage(chains, kind)
    with pytest.raises((ValueError, OSError)):
        _assemble(chains, tmp_path / 'evidence.json')
    assert not (tmp_path / 'evidence.json').exists()


def test_cli_success_and_failure(tmp_path, capsys):
    chains = _chains(tmp_path)
    artifacts = _artifacts(chains)
    output = tmp_path / 'evidence.json'
    args = ['--frozen-export-manifest', str(artifacts['frozen_export']),
            '--other-catalog-disposition', str(artifacts['other_catalog_disposition']),
            '--document-reverse-manifest', str(artifacts['document_reverse']),
            '--wiki-reverse-manifest', str(artifacts['wiki_reverse']),
            '--output', str(output)]
    assert evidence.main(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['format'] == evidence.FORMAT and result['state'] == 'verified_rollback_evidence'
    assert output.is_file()
    second = tmp_path / 'second.json'
    args[-1] = str(second)
    _tamper(artifacts['wiki_reverse'], lambda payload: payload.update(activation_allowed=True))
    assert evidence.main(args) == 1
    failure = json.loads(capsys.readouterr().out)
    assert failure['error_code'] == 'rollback_evidence_rejected'
    for flag in evidence.FLAGS:
        assert failure[flag] is False
    assert str(tmp_path) not in json.dumps(failure)
    assert not second.exists()
