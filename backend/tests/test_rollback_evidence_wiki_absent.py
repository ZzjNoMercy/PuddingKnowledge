import hashlib
import json

import pytest

from knowledge_platform.distribution import rollback_evidence as evidence
from knowledge_platform.distribution import wiki_reverse_absent as absent
from knowledge_platform.distribution.wiki_reverse_absent import attest_wiki_absent
from knowledge_platform.local.writer_authority import digest, encoded
from test_rollback_evidence import _document_lineage, _tamper
from test_wiki_reverse_absent import _export as _wikiless_export


def _chains(tmp_path):
    """Real Catalog chain plus a Wiki-absent attestation for the fourth receipt."""
    chains = _document_lineage(tmp_path / 'documents')
    export = _wikiless_export(tmp_path / 'wiki-absent')
    attestation = tmp_path / 'attestation'
    assert attest_wiki_absent(export, attestation, source_revision='legacy-1')['state'] == 'verified_absent_wiki'
    chains['wiki_absent'] = attestation
    return chains


def _assemble(chains, output):
    return evidence.assemble_rollback_evidence(
        chains['export'] / 'manifest.json', chains['disposition'],
        chains['reverse'] / 'manifest.json', chains['wiki_absent'] / 'manifest.json', output)


def test_absent_table_contract_mirrors_the_producer():
    assert evidence.WIKI_ABSENT_TABLES == absent.INSPECTED_TABLES


def test_absent_attestation_assembles_deterministic_evidence(tmp_path):
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
    expected = [('frozen_export', 'verified_frozen_export'),
                ('other_catalog_disposition', 'verified_other_catalog_disposition'),
                ('document_reverse', 'verified_inactive_documents'),
                ('wiki_reverse', 'verified_absent_wiki')]
    assert [(entry['role'], entry['state']) for entry in result['artifacts']] == expected
    assert result['artifacts'][3]['sha256'] == \
        hashlib.sha256((chains['wiki_absent'] / 'manifest.json').read_bytes()).hexdigest()
    assert result['linkages']['wiki_domain_attested_absent'] is True
    assert result['artifact_digests']['wiki_brain_inventory_sha256'] == digest({'files': {}, 'directories': []})
    for flag in evidence.FLAGS:
        assert result[flag] is False


def test_absent_exact_retry_is_byte_identical(tmp_path):
    chains = _chains(tmp_path)
    output = tmp_path / 'evidence.json'
    first = _assemble(chains, output)
    data = output.read_bytes()
    second = _assemble(chains, output)
    assert second == {**first, 'idempotent': True}
    assert output.read_bytes() == data


def test_absent_source_revision_linkage_is_enforced(tmp_path):
    chains = _chains(tmp_path)
    _tamper(chains['wiki_absent'] / 'manifest.json',
            lambda payload: payload['plan'].update(source_revision='legacy-other'))
    with pytest.raises(ValueError, match='disagree on the source snapshot revision'):
        _assemble(chains, tmp_path / 'evidence.json')
    assert not (tmp_path / 'evidence.json').exists()


def test_attestation_produced_with_another_revision_refuses(tmp_path):
    chains = _document_lineage(tmp_path / 'documents')
    export = _wikiless_export(tmp_path / 'wiki-absent')
    attestation = tmp_path / 'attestation'
    assert attest_wiki_absent(export, attestation, source_revision='legacy-other')['state'] == 'verified_absent_wiki'
    chains['wiki_absent'] = attestation
    with pytest.raises(ValueError, match='disagree on the source snapshot revision'):
        _assemble(chains, tmp_path / 'evidence.json')


@pytest.mark.parametrize('tamper', ['format', 'state', 'flag', 'noncanonical'])
def test_absent_envelope_tamper_refuses(tmp_path, tamper):
    chains = _chains(tmp_path)
    path = chains['wiki_absent'] / 'manifest.json'
    if tamper == 'noncanonical':
        path.write_bytes(json.dumps(json.loads(path.read_bytes()), indent=2).encode())
        path.chmod(0o600)
    elif tamper == 'format':
        _tamper(path, lambda payload: payload.update(format='puddingknowledge-forged/v9'))
    elif tamper == 'state':
        _tamper(path, lambda payload: payload.update(state='verified_inactive_wiki'))
    else:
        _tamper(path, lambda payload: payload.update(rollback_completed=True))
    with pytest.raises(ValueError):
        _assemble(chains, tmp_path / 'evidence.json')
    assert not (tmp_path / 'evidence.json').exists()


def _plan_tamper(chains, kind):
    path = chains['wiki_absent'] / 'manifest.json'
    if kind == 'workspace_manifest':
        _tamper(path, lambda payload: payload['plan']['inputs']['current_workspace'].update(manifest_sha256='z'))
    elif kind == 'workspace_catalog':
        _tamper(path, lambda payload: payload['plan']['inputs']['current_workspace'].update(catalog_sha256='z'))
    elif kind == 'output_identity':
        _tamper(path, lambda payload: payload['plan'].update(output_identity={'path': '/tmp/elsewhere',
                                                                              'device': 0, 'inode': 0}))
    elif kind == 'table_rows':
        _tamper(path, lambda payload: payload['plan']['absence']['tables'][0].update(rows=1))
    elif kind == 'table_name':
        _tamper(path, lambda payload: payload['plan']['absence']['tables'][3].update(table='knowledge_other'))
    elif kind == 'table_member':
        _tamper(path, lambda payload: payload['plan']['absence']['tables'][0].pop('present'))
    elif kind == 'evidence_flag':
        _tamper(path, lambda payload: payload['plan']['absence'].update(wiki_evidence_present=True))
    elif kind == 'evidence_member':
        _tamper(path, lambda payload: payload['plan']['absence'].pop('wiki_schema_present'))
    else:
        raise AssertionError(kind)


@pytest.mark.parametrize('kind', ['workspace_manifest', 'workspace_catalog', 'output_identity',
                                  'table_rows', 'table_name', 'table_member',
                                  'evidence_flag', 'evidence_member'])
def test_absent_plan_tamper_refuses(tmp_path, kind):
    chains = _chains(tmp_path)
    _plan_tamper(chains, kind)
    with pytest.raises(ValueError):
        _assemble(chains, tmp_path / 'evidence.json')
    assert not (tmp_path / 'evidence.json').exists()


def test_absent_receipt_cannot_masquerade_as_active(tmp_path):
    chains = _chains(tmp_path)
    _tamper(chains['wiki_absent'] / 'manifest.json',
            lambda payload: payload.update(format=evidence.WIKI_FORMAT, state=evidence.WIKI_STATE))
    with pytest.raises(ValueError, match='Not a verified inactive Wiki candidate receipt'):
        _assemble(chains, tmp_path / 'evidence.json')
    assert not (tmp_path / 'evidence.json').exists()


def test_evidence_must_live_outside_the_attestation(tmp_path):
    chains = _chains(tmp_path)
    with pytest.raises(ValueError, match='outside the step outputs'):
        _assemble(chains, chains['wiki_absent'] / 'evidence.json')


def test_absent_cli_success_and_failure(tmp_path, capsys):
    chains = _chains(tmp_path)
    output = tmp_path / 'evidence.json'
    args = ['--frozen-export-manifest', str(chains['export'] / 'manifest.json'),
            '--other-catalog-disposition', str(chains['disposition']),
            '--document-reverse-manifest', str(chains['reverse'] / 'manifest.json'),
            '--wiki-reverse-manifest', str(chains['wiki_absent'] / 'manifest.json'),
            '--output', str(output)]
    assert evidence.main(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['format'] == evidence.FORMAT and result['state'] == 'verified_rollback_evidence'
    assert result['linkages']['wiki_domain_attested_absent'] is True
    second = tmp_path / 'second.json'
    args[-1] = str(second)
    _tamper(chains['wiki_absent'] / 'manifest.json', lambda payload: payload.update(activation_allowed=True))
    assert evidence.main(args) == 1
    failure = json.loads(capsys.readouterr().out)
    assert failure['error_code'] == 'rollback_evidence_rejected'
    for flag in evidence.FLAGS:
        assert failure[flag] is False
    assert str(tmp_path) not in json.dumps(failure)
    assert not second.exists()
