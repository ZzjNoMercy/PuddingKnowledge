import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

import pytest
from knowledge_platform.distribution.document_migration import prepare_document_migration
from knowledge_platform.distribution.document_reverse import prepare_document_reverse
from knowledge_platform.distribution.installation_path_rebind import (
    FORMAT, PART, BACKUP, rebind_installation_paths, main,
)
from knowledge_platform.catalog.rehearsal_runner import _digest
from test_core_catalog_reverse import LEGACY_SCHEMA, fixture
from test_document_migration import _legacy_catalog


def make_candidate(tmp_path, *, new=True):
    source, before, after, _ = fixture(tmp_path, real=True)
    bodies = tmp_path/'current-bodies'; bodies.mkdir(mode=0o700)
    content = b'# Updated body\n'; (bodies/'updated.md').write_bytes(content)
    with sqlite3.connect(after) as db:
        db.row_factory = sqlite3.Row
        original = dict(db.execute('SELECT * FROM knowledge_assets').fetchone())
        digest = 'sha256:'+hashlib.sha256(content).hexdigest()
        db.execute('UPDATE knowledge_assets SET content_digest=?,revision=?,title=?', (digest, digest, 'Current title'))
        bindings = {original['id']: 'updated.md'}
        if new:
            native = dict(original, id='native-document-2', title='New native document',
                          source_uri='knowledge://spaces/space_kb-1/assets/native-document-2', metadata_json='{"tag":"new"}')
            native['content_digest'] = native['revision'] = 'sha256:'+hashlib.sha256(b'new body').hexdigest()
            db.execute('INSERT INTO knowledge_assets ('+','.join(native)+') VALUES ('+','.join('?' for _ in native)+')', tuple(native.values()))
            (bodies/'new.md').write_bytes(b'new body'); bindings[native['id']] = 'new.md'
            dataset = dict(db.execute('SELECT * FROM knowledge_datasets').fetchone())
            ids = json.loads(dataset['asset_ids'])+[native['id']]
            manifest = _digest({'asset_ids': ids, 'source_revision': 'legacy-1', 'version': dataset['version']})
            db.execute('UPDATE knowledge_datasets SET asset_ids=?,manifest_digest=?', (json.dumps(ids), manifest))
    output = tmp_path/'reverse'
    prepare_document_reverse(source, before, after, bodies, bindings, output, source_revision='legacy-1')
    return output


def attachment_candidate(tmp_path):
    source = tmp_path/'source.sqlite3'
    body = b'unchanged content'
    _legacy_catalog(source, content_digest=hashlib.sha256(body).hexdigest())
    with sqlite3.connect(source) as db:
        db.execute('ALTER TABLE knowledge_documents ADD COLUMN publish_targets JSON')
        db.execute('ALTER TABLE knowledge_documents ADD COLUMN size_bytes INTEGER')
        metadata = {'nested': {'token': 'private-value', 'label': 'old'}, 'origin': 'original',
                    'assets': [{'path': '/shared/picture.png', 'virtual_path': '/knowledge/old.png'}]}
        db.execute('UPDATE knowledge_documents SET doc_metadata=?, publish_targets=?, size_bytes=?',
                   (json.dumps(metadata), '["wiki"]', len(body)))
        db.executescript('CREATE TABLE unrelated(id TEXT PRIMARY KEY, value TEXT); INSERT INTO unrelated VALUES ("u1","keep-me");')
    full = tmp_path/'full.sqlite3'
    with sqlite3.connect(source) as old, sqlite3.connect(full) as db:
        old.row_factory = sqlite3.Row
        db.executescript(LEGACY_SCHEMA.read_text())
        for table in ('knowledge_bases', 'knowledge_documents'):
            for original in old.execute('SELECT * FROM '+table):
                row = dict(original)
                db.execute('INSERT INTO '+table+' ('+','.join(row)+') VALUES ('+','.join('?' for _ in row)+')', tuple(row.values()))
    full.replace(source)
    files_dir = tmp_path/'files'; files_dir.mkdir(); (files_dir/'body.md').write_bytes(body)
    (files_dir/'attachments').mkdir(); (files_dir/'attachments'/'picture.png').write_bytes(b'picture-bytes')
    package = tmp_path/'package'
    prepare_document_migration(source, files_dir, {'doc-1': 'body.md'}, package, source_revision='legacy-1',
                               attachment_bindings={'/shared/picture.png': 'attachments/picture.png'})
    before = package/'catalog.sqlite3'; after = tmp_path/'after.sqlite3'; shutil.copyfile(before, after)
    bodies = tmp_path/'current-bodies'; bodies.mkdir(mode=0o700)
    (bodies/'updated.md').write_bytes(body)
    (bodies/'attachments').mkdir(); (bodies/'attachments'/'picture.png').write_bytes(b'picture-bytes')
    with sqlite3.connect(after) as db:
        asset = db.execute('SELECT id,metadata_json FROM knowledge_assets').fetchone()
        assert json.loads(asset[1])['assets'][0]['path'] == '/shared/picture.png'
        bindings = {asset[0]: 'updated.md'}
    output = tmp_path/'reverse'
    prepare_document_reverse(source, before, after, bodies, bindings, output, source_revision='legacy-1',
                             attachment_bindings={'/shared/picture.png': 'attachments/picture.png'})
    return output


def moved(tmp_path, *, new=True):
    installed = tmp_path/'installed'
    make_candidate(tmp_path, new=new).rename(installed)
    return installed, tmp_path/'receipt.json'


def run(candidate, receipt, **kwargs):
    return rebind_installation_paths(candidate, candidate/'manifest.json', receipt, **kwargs)


def pin(candidate):
    return json.loads((candidate/'manifest.json').read_text())['catalog_sha256']


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def rows(candidate):
    uri = 'file:{}?mode=ro'.format(candidate/'catalog.sqlite3')
    with sqlite3.connect(uri, uri=True) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute('SELECT * FROM knowledge_documents')]


def recommit(candidate, mutate):
    catalog = candidate/'catalog.sqlite3'
    with sqlite3.connect(catalog) as db:
        mutate(db)
    marker = candidate/'manifest.json'
    manifest = json.loads(marker.read_text())
    manifest['catalog_sha256'] = digest(catalog)
    marker.write_bytes((json.dumps(manifest, sort_keys=True, separators=(',', ':'), ensure_ascii=False)+'\n').encode())


def rewrite_manifest(candidate, mutate):
    marker = candidate/'manifest.json'
    manifest = json.loads(marker.read_text())
    mutate(manifest)
    marker.write_bytes((json.dumps(manifest, sort_keys=True, separators=(',', ':'), ensure_ascii=False)+'\n').encode())


def test_moved_candidate_rebinds_and_verifies(tmp_path):
    candidate, receipt_path = moved(tmp_path)
    old_prefix = str(tmp_path/'reverse'/'bodies')
    result = run(candidate, receipt_path)
    assert result['idempotent'] is False and result['state'] == 'verified_installation_path_rebound'
    assert result['format'] == FORMAT and result['installation_path_rebound'] is True
    assert result['old_prefix'] == old_prefix and result['new_prefix'] == str(candidate/'bodies')
    assert result['rebound_fields'] == {'knowledge_documents.doc_metadata.assets[*].path': 0,
                                        'knowledge_documents.doc_metadata.multimodal.image_assets_dir': 0,
                                        'knowledge_documents.doc_metadata.original_path': 0,
                                        'knowledge_documents.storage_path': 2}
    assert result['documents'] == 2 and result['bodies_verified'] == 2
    assert result['catalog_sha256_before'] == pin(candidate)
    assert result['catalog_sha256_after'] == digest(candidate/'catalog.sqlite3')
    assert result['catalog_sha256_after'] != result['catalog_sha256_before']
    assert result['receipt_sha256'] == digest(receipt_path)
    for flag in ('indexes_rebuilt', 'rollback_completed', 'activation_allowed', 'installation_cutover_performed'):
        assert result[flag] is False
    manifest = json.loads((candidate/'manifest.json').read_text())
    bodies = {fact['output_relative']: fact for fact in manifest['plan']['bodies'].values()}
    for row in rows(candidate):
        storage = Path(row['storage_path'])
        assert storage.is_relative_to(candidate/'bodies') and not str(storage).startswith(old_prefix)
        relative = storage.relative_to(candidate).as_posix()
        assert hashlib.sha256(storage.read_bytes()).hexdigest() == bodies[relative]['sha256']
        assert storage.stat().st_size == bodies[relative]['size_bytes']
        assert row['source_path'] == 'docs/readme.md' or json.loads(row['doc_metadata']).get('tag') == 'new'
    raw = receipt_path.read_bytes()
    assert json.loads(raw) == {key: value for key, value in result.items() if key not in ('idempotent', 'receipt_sha256')}


def test_exact_retry_is_byte_identical_and_idempotent(tmp_path):
    candidate, receipt_path = moved(tmp_path)
    result = run(candidate, receipt_path)
    raw, catalog_digest = receipt_path.read_bytes(), digest(candidate/'catalog.sqlite3')
    again = run(candidate, receipt_path)
    assert again == dict(result, idempotent=True)
    assert receipt_path.read_bytes() == raw and digest(candidate/'catalog.sqlite3') == catalog_digest


def test_copied_candidate_is_detected_and_rebound(tmp_path):
    original = make_candidate(tmp_path)
    installed = tmp_path/'installed'
    shutil.copytree(original, installed)
    # The old tree still resolves; the rebind must detect the prefix mismatch
    # rather than accepting paths that happen to exist.
    result = run(installed, tmp_path/'receipt.json')
    assert result['new_prefix'] == str(installed/'bodies') and not result['idempotent']
    for row in rows(installed):
        assert Path(row['storage_path']).is_relative_to(installed/'bodies')
    for row in rows(original):
        assert Path(row['storage_path']).is_relative_to(original/'bodies')
    assert digest(original/'catalog.sqlite3') == pin(original)


def test_body_digest_mismatch_at_new_location_refuses(tmp_path):
    candidate, receipt_path = moved(tmp_path, new=False)
    committed = digest(candidate/'catalog.sqlite3')
    (candidate/'bodies'/'updated.md').write_bytes(b'corrupted at the new location')
    with pytest.raises(ValueError, match='bodies changed'):
        run(candidate, receipt_path)
    assert digest(candidate/'catalog.sqlite3') == committed
    assert not receipt_path.exists() and not (candidate/PART).exists() and not (candidate/BACKUP).exists()


def test_divergent_storage_prefix_refuses(tmp_path):
    candidate, receipt_path = moved(tmp_path, new=False)
    recommit(candidate, lambda db: db.execute("UPDATE knowledge_documents SET storage_path='/other/root/updated.md'"))
    committed = digest(candidate/'catalog.sqlite3')
    with pytest.raises(ValueError, match='escaped its declared root'):
        run(candidate, receipt_path)
    assert digest(candidate/'catalog.sqlite3') == committed and not receipt_path.exists()


def test_escaped_storage_prefix_refuses(tmp_path):
    candidate, receipt_path = moved(tmp_path, new=False)
    escaped = str(tmp_path/'reverse'/'bodies'/'..'/'escape.md')
    recommit(candidate, lambda db: db.execute('UPDATE knowledge_documents SET storage_path=?', (escaped,)))
    committed = digest(candidate/'catalog.sqlite3')
    with pytest.raises(ValueError, match='canonical'):
        run(candidate, receipt_path)
    assert digest(candidate/'catalog.sqlite3') == committed and not receipt_path.exists()


@pytest.mark.parametrize('tamper', ['state', 'inert-flag', 'commitment', 'canonical'])
def test_receipt_tampering_refuses(tmp_path, tamper):
    candidate, receipt_path = moved(tmp_path, new=False)
    committed = digest(candidate/'catalog.sqlite3')
    if tamper == 'state':
        rewrite_manifest(candidate, lambda manifest: manifest.update(state='copying'))
    elif tamper == 'inert-flag':
        rewrite_manifest(candidate, lambda manifest: manifest.update(installation_path_rebound=True))
    elif tamper == 'commitment':
        rewrite_manifest(candidate, lambda manifest: manifest.update(catalog_sha256='0'*64))
    else:
        marker = candidate/'manifest.json'
        marker.write_text(json.dumps(json.loads(marker.read_text()), indent=2, sort_keys=True))
    with pytest.raises(ValueError):
        run(candidate, receipt_path)
    assert digest(candidate/'catalog.sqlite3') == committed and not receipt_path.exists()


def test_catalog_digest_drift_refuses(tmp_path):
    candidate, receipt_path = moved(tmp_path, new=False)
    with sqlite3.connect(candidate/'catalog.sqlite3') as db:
        db.execute("UPDATE knowledge_documents SET title='edited outside the audited step'")
    drifted = digest(candidate/'catalog.sqlite3')
    assert drifted != pin(candidate)
    with pytest.raises(ValueError, match='drifted'):
        run(candidate, receipt_path)
    assert digest(candidate/'catalog.sqlite3') == drifted and not receipt_path.exists()
    assert not (candidate/PART).exists() and not (candidate/BACKUP).exists()


def test_crash_between_catalog_publish_and_receipt_recovers(tmp_path):
    candidate, receipt_path = moved(tmp_path, new=False)
    ready = tmp_path/'ready'
    code = ('import os\n'
            'from knowledge_platform.distribution.installation_path_rebind import rebind_installation_paths\n'
            'def crash():\n'
            '    open(%r, "w").close()\n'
            '    os._exit(42)\n'
            'rebind_installation_paths(%r, %r, %r, _after_publish=crash)\n'
            % (str(ready), str(candidate), str(candidate/'manifest.json'), str(receipt_path)))
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    child = subprocess.run([sys.executable, '-c', code], env=env, capture_output=True)
    assert child.returncode == 42 and ready.exists(), child.stderr.decode()
    assert not receipt_path.exists()
    rebound = digest(candidate/'catalog.sqlite3')
    assert rebound != pin(candidate) and digest(candidate/BACKUP) == pin(candidate)
    result = run(candidate, receipt_path)
    assert result['idempotent'] is False
    assert result['catalog_sha256_before'] == pin(candidate) and result['catalog_sha256_after'] == rebound
    assert not (candidate/BACKUP).exists()
    assert run(candidate, receipt_path) == dict(result, idempotent=True)


def test_interrupted_rewrite_part_is_rebuilt(tmp_path):
    candidate, receipt_path = moved(tmp_path, new=False)
    part = candidate/PART
    part.write_bytes(b'interrupted rewrite'); part.chmod(0o600)
    result = run(candidate, receipt_path)
    assert result['catalog_sha256_after'] == digest(candidate/'catalog.sqlite3')
    assert not part.exists()


def test_interrupted_receipt_part_is_republished(tmp_path):
    candidate, receipt_path = moved(tmp_path, new=False)
    part = receipt_path.parent/(receipt_path.name+'.part')
    part.write_bytes(b'interrupted receipt'); part.chmod(0o600)
    result = run(candidate, receipt_path)
    assert result['receipt_sha256'] == digest(receipt_path) and not part.exists()


def test_unmoved_candidate_verifies_without_rewrite(tmp_path):
    candidate = make_candidate(tmp_path)
    receipt_path = tmp_path/'receipt.json'
    result = run(candidate, receipt_path)
    assert result['old_prefix'] == result['new_prefix'] == str(candidate/'bodies')
    assert result['catalog_sha256_before'] == result['catalog_sha256_after'] == pin(candidate)
    assert set(result['rebound_fields'].values()) == {0}
    assert result['idempotent'] is False
    assert run(candidate, receipt_path) == dict(result, idempotent=True)
    assert digest(candidate/'catalog.sqlite3') == pin(candidate)


def test_receipt_inside_candidate_refuses(tmp_path):
    candidate, _ = moved(tmp_path, new=False)
    with pytest.raises(ValueError, match='outside the candidate'):
        run(candidate, candidate/'receipt.json')


def test_existing_disagreeing_receipt_refuses(tmp_path):
    candidate, receipt_path = moved(tmp_path, new=False)
    result = run(candidate, receipt_path)
    tampered = dict(json.loads(receipt_path.read_bytes()), bodies_verified=0)
    receipt_path.write_bytes((json.dumps(tampered, sort_keys=True, separators=(',', ':'), ensure_ascii=False)+'\n').encode())
    with pytest.raises(ValueError, match='disagrees'):
        run(candidate, receipt_path)
    assert digest(candidate/'catalog.sqlite3') == result['catalog_sha256_after']


def test_attachment_metadata_paths_are_rebound(tmp_path):
    installed = tmp_path/'installed'
    attachment_candidate(tmp_path).rename(installed)
    result = run(installed, tmp_path/'receipt.json')
    (row,) = rows(installed)
    metadata = json.loads(row['doc_metadata'])
    assert metadata['assets'][0]['path'] == str(installed/'bodies'/'attachments'/'picture.png')
    assert row['storage_path'] == str(installed/'bodies'/'updated.md')
    assert metadata['assets'][0]['virtual_path'] == '/knowledge/attachments/picture.png'
    assert result['rebound_fields']['knowledge_documents.doc_metadata.assets[*].path'] == 1
    assert result['rebound_fields']['knowledge_documents.storage_path'] == 1
    assert result['bodies_verified'] == 2
    assert run(installed, tmp_path/'receipt.json') == dict(result, idempotent=True)


def test_cli_success_and_error(tmp_path, capsys):
    candidate, receipt_path = moved(tmp_path, new=False)
    argv = ['--candidate', str(candidate), '--manifest', str(candidate/'manifest.json'), '--output', str(receipt_path)]
    assert main(argv) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed['format'] == FORMAT and printed['state'] == 'verified_installation_path_rebound'
    assert main(['--candidate', str(candidate), '--manifest', str(candidate/'manifest.json'),
                 '--output', str(candidate/'receipt.json')]) == 1
    printed = json.loads(capsys.readouterr().out)
    assert printed['error_code'] == 'installation_path_rebind_rejected' and printed['installation_path_rebound'] is False
