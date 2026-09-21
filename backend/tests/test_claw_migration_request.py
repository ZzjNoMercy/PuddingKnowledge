import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import sys

import pytest
from test_pdf_representation_migration import pdf_bytes
from knowledge_platform.distribution.claw_migration_request import FORMAT, generate_migration_request
from knowledge_platform.distribution.migrate_from_claw import REQUEST_FORMAT_V2, migrate_from_claw

LEGACY = '/Users/pet/Documents/knowledge'
BODY = b'# Readme\n\nportable content\n'
PDF_BODY = b'# Parsed paper\n\nconverted body\n'
PDF = pdf_bytes()
VISION = b'# Vision note\n'
CHART = b'chart-bytes'
EXTRA = b'extra\n'
PAYLOAD = {'external/knowledge/imported/readme.md': BODY, 'external/knowledge/imported/paper.md': PDF_BODY,
           'external/knowledge/imported/paper.pdf': PDF, 'external/knowledge/imported/vision.md': VISION,
           'external/knowledge/assets/chart.png': CHART, 'external/knowledge/assets/extra.txt': EXTRA}


def _catalog(path, documents):
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE knowledge_bases (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT,
            created_at DATETIME, updated_at DATETIME
        );
        CREATE TABLE knowledge_documents (
            id TEXT PRIMARY KEY, knowledge_base_id TEXT NOT NULL,
            title TEXT NOT NULL, source_type TEXT, source_path TEXT,
            storage_path TEXT, virtual_path TEXT, mime_type TEXT,
            content_sha256 TEXT, size_bytes INTEGER, status TEXT,
            publish_targets JSON, doc_metadata JSON,
            origin_url TEXT, created_at DATETIME, updated_at DATETIME
        );
        INSERT INTO knowledge_bases VALUES
          ('kb-1', 'Docs', 'migrated', '2026-09-11 00:00:00.000000', '2026-09-11 00:00:00.000000');
        """
    )
    for doc in documents:
        db.execute(
            """INSERT INTO knowledge_documents
            (id, knowledge_base_id, title, source_type, source_path, storage_path,
             virtual_path, mime_type, content_sha256, size_bytes, status,
             publish_targets, doc_metadata, origin_url, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (doc['id'], 'kb-1', doc.get('title', doc['id']), doc.get('source_type', 'local'),
             doc.get('source_path'), doc['storage_path'], doc.get('virtual_path', doc['id'] + '.md'),
             doc.get('mime_type', 'text/markdown'), doc['content_sha256'], doc.get('size_bytes'),
             'published', '[]', json.dumps(doc.get('doc_metadata') or {}), '',
             '2026-09-11 00:00:00.000000', '2026-09-11 00:00:00.000000'),
        )
    db.commit()
    db.close()


def _documents():
    return [
        {'id': 'doc-1', 'source_path': LEGACY + '/imported/readme.md',
         'storage_path': LEGACY + '/imported/readme.md',
         'content_sha256': hashlib.sha256(BODY).hexdigest()},
        {'id': 'doc-2', 'source_type': 'pdf_mineru', 'source_path': LEGACY + '/imported/paper.pdf',
         'storage_path': LEGACY + '/imported/paper.md',
         'content_sha256': hashlib.sha256(PDF).hexdigest(), 'size_bytes': len(PDF_BODY),
         'doc_metadata': {'mode': 'multimodal_pdf', 'original_path': LEGACY + '/imported/paper.pdf',
                          'original_sha256': hashlib.sha256(PDF).hexdigest(),
                          'markdown_sha256': hashlib.sha256(PDF_BODY).hexdigest()}},
        {'id': 'doc-3', 'source_path': LEGACY + '/imported/vision.md',
         'storage_path': LEGACY + '/imported/vision.md',
         'content_sha256': hashlib.sha256(VISION).hexdigest(),
         'doc_metadata': {'assets': [{'path': LEGACY + '/assets/chart.png',
                                      'sha256': hashlib.sha256(CHART).hexdigest(), 'size_bytes': len(CHART)}],
                          'multimodal': {'image_assets_dir': LEGACY + '/assets'},
                          'route': '/knowledge/documents/doc-3'}},
    ]


def _snapshot(root, *, documents=None, payload=None, wiki=True):
    snapshot = root / 'snapshot'
    snapshot.mkdir()
    files = snapshot / 'payload'
    (files / 'external/knowledge/assets/empty').mkdir(parents=True)
    for relative, data in (PAYLOAD if payload is None else payload).items():
        path = files / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    _catalog(snapshot / 'legacy.sqlite3', _documents() if documents is None else documents)
    brain = snapshot / 'brain'
    brain.mkdir()
    if wiki:
        (brain / 'raw').mkdir()
        (brain / 'wiki').mkdir()
        (brain / 'wiki/index.md').write_text('# Index')
        (brain / 'raw/data.md').write_bytes(b'raw')
        (brain / 'raw/manifest.jsonl').write_text(
            json.dumps({'snapshot_path': 'data.md', 'sha256': hashlib.sha256(b'raw').hexdigest(), 'size_bytes': 3}) + '\n')
    return snapshot, snapshot / 'legacy.sqlite3', files, brain


def _generate(root, parts, **overrides):
    snapshot, catalog, files, wiki = parts
    arguments = {'installation_id': 'install-1', 'source_revision': 'legacy-1',
                 'source_schema_revision': 'legacy-v1', 'mappings': [LEGACY + '=external/knowledge'],
                 'output': root / 'request.json', 'receipt': root / 'receipt.json'}
    arguments.update(overrides)
    return generate_migration_request(snapshot, catalog, files, wiki, **arguments)


def test_happy_path_generates_preverified_v2_request_and_chain_succeeds(tmp_path):
    parts = _snapshot(tmp_path)
    snapshot, catalog, files, wiki = parts
    result = _generate(tmp_path, parts)
    request_path = tmp_path / 'request.json'
    request_bytes = request_path.read_bytes()
    request = json.loads(request_bytes)
    assert set(request) == {'format', 'installation_id', 'source_revision', 'source_schema_revision',
                            'source_catalog', 'source_files_root', 'source_wiki_root',
                            'bindings', 'original_bindings', 'attachment_bindings'}
    assert request['format'] == REQUEST_FORMAT_V2
    assert request['bindings'] == {'doc-1': 'external/knowledge/imported/readme.md',
                                   'doc-2': 'external/knowledge/imported/paper.md',
                                   'doc-3': 'external/knowledge/imported/vision.md'}
    assert request['original_bindings'] == {'doc-2': 'external/knowledge/imported/paper.pdf'}
    assert request['attachment_bindings'] == {LEGACY + '/imported/paper.pdf': 'external/knowledge/imported/paper.pdf',
                                              LEGACY + '/assets/chart.png': 'external/knowledge/assets/chart.png',
                                              LEGACY + '/assets': 'external/knowledge/assets'}
    assert (request['source_catalog'], request['source_files_root'], request['source_wiki_root']) == (str(catalog), str(files), str(wiki))
    canonical = json.dumps(request, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode() + b'\n'
    assert request_bytes == canonical
    for path in (request_path, tmp_path / 'receipt.json'):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    base = len(BODY) + len(PDF_BODY) + len(PDF) + len(VISION)
    assert result['counts'] == {'documents': 3, 'originals': 1, 'attachments': 3,
                                'bytes': 2 * base + len(CHART) + len(EXTRA)}
    assert result['unmapped_references'] == 0 and result['activation_allowed'] is False
    assert result['request_digest'] == 'sha256:' + hashlib.sha256(request_bytes).hexdigest()
    assert result['catalog_digest'] == 'sha256:' + hashlib.sha256(catalog.read_bytes()).hexdigest()

    output = tmp_path / 'delegate'
    receipt = migrate_from_claw(request_path, output, source_snapshot=snapshot)
    assert receipt['state'] == 'verified_inactive_partial'
    assert 'wiki_archive' in receipt['covered_domains'] and 'wiki' in receipt['pending_domains']
    assert 'candidate/resources/external/knowledge/assets/extra.txt' in receipt['artifacts']
    assert 'candidate/resources/external/knowledge/assets/empty' not in receipt['artifacts']
    assert 'wiki/archive/wiki/index.md' in receipt['artifacts']
    for name, digest in receipt['artifacts'].items():
        assert 'sha256:' + hashlib.sha256((output / name).read_bytes()).hexdigest() == digest
    assert migrate_from_claw(request_path, output, source_snapshot=snapshot) == receipt


def test_empty_wiki_root_still_completes_the_forward_chain(tmp_path):
    parts = _snapshot(tmp_path, wiki=False)
    _generate(tmp_path, parts)
    output = tmp_path / 'delegate'
    receipt = migrate_from_claw(tmp_path / 'request.json', output, source_snapshot=parts[0])
    assert receipt['state'] == 'verified_inactive_partial'
    assert 'wiki_archive' in receipt['covered_domains']
    assert 'wiki/manifest.json' in receipt['artifacts']
    assert not any(name.startswith('wiki/archive/') for name in receipt['artifacts'])


def test_committed_digest_mismatch_refuses_before_publication(tmp_path):
    parts = _snapshot(tmp_path)
    (parts[2] / 'external/knowledge/imported/readme.md').write_bytes(b'changed')
    with pytest.raises(ValueError, match='digest mismatch'):
        _generate(tmp_path, parts)
    assert not (tmp_path / 'request.json').exists() and not (tmp_path / 'receipt.json').exists()


def test_unmapped_source_reference_refuses(tmp_path):
    documents = _documents()
    documents[0]['source_path'] = documents[0]['storage_path'] = '/elsewhere/readme.md'
    parts = _snapshot(tmp_path, documents=documents)
    with pytest.raises(ValueError, match='Unmapped'):
        _generate(tmp_path, parts)
    assert not (tmp_path / 'request.json').exists()


def test_mapping_collision_refuses(tmp_path):
    documents = [
        {'id': 'doc-1', 'source_path': '/a/x.md', 'storage_path': '/a/x.md',
         'content_sha256': hashlib.sha256(b'x').hexdigest()},
        {'id': 'doc-2', 'source_path': '/b/x.md', 'storage_path': '/b/x.md',
         'content_sha256': hashlib.sha256(b'x').hexdigest()},
    ]
    parts = _snapshot(tmp_path, documents=documents, payload={'payload/x.md': b'x'})
    with pytest.raises(ValueError, match='collide'):
        _generate(tmp_path, parts, mappings=['/a=payload', '/b=payload'])


@pytest.mark.parametrize('relative', ['../escape', '/absolute', 'a\\b', 'a//b', '.', 'a/./b'])
def test_hostile_mapping_destination_refuses(tmp_path, relative):
    parts = _snapshot(tmp_path)
    with pytest.raises(ValueError):
        _generate(tmp_path, parts, mappings=[LEGACY + '=' + relative])
    assert not (tmp_path / 'request.json').exists()


@pytest.mark.parametrize('field,value', [('installation_id', 'bad id'), ('source_revision', '-bad'),
                                         ('source_schema_revision', 'x' * 81), ('source_schema_revision', '')])
def test_non_token_identity_refuses(tmp_path, field, value):
    parts = _snapshot(tmp_path)
    with pytest.raises(ValueError, match='identity'):
        _generate(tmp_path, parts, **{field: value})


@pytest.mark.parametrize('escape', ['catalog', 'files', 'wiki'])
def test_sources_outside_snapshot_refuse(tmp_path, escape):
    snapshot, catalog, files, wiki = _snapshot(tmp_path)
    outside = tmp_path / 'outside'
    if escape == 'catalog':
        outside.write_bytes(catalog.read_bytes())
        catalog = outside
    elif escape == 'files':
        shutil.copytree(files, outside)
        files = outside
    else:
        outside.mkdir()
        wiki = outside
    with pytest.raises(ValueError, match='approved snapshot'):
        generate_migration_request(snapshot, catalog, files, wiki, installation_id='install-1',
                                   source_revision='legacy-1', source_schema_revision='legacy-v1',
                                   mappings=[LEGACY + '=external/knowledge'],
                                   output=tmp_path / 'request.json', receipt=tmp_path / 'receipt.json')


def test_empty_catalog_refuses(tmp_path):
    parts = _snapshot(tmp_path, documents=[])
    with pytest.raises(ValueError, match='no documents'):
        _generate(tmp_path, parts)


def test_unquiesced_catalog_sidecar_refuses(tmp_path):
    parts = _snapshot(tmp_path)
    Path(str(parts[1]) + '-wal').write_bytes(b'wal')
    with pytest.raises(ValueError, match='quiesced'):
        _generate(tmp_path, parts)
    assert not (tmp_path / 'request.json').exists()


def test_outputs_are_deterministic_and_republish_without_replacement(tmp_path):
    parts = _snapshot(tmp_path)
    first = _generate(tmp_path, parts)
    request_bytes = (tmp_path / 'request.json').read_bytes()
    receipt_bytes = (tmp_path / 'receipt.json').read_bytes()
    assert _generate(tmp_path, parts) == first
    assert (tmp_path / 'request.json').read_bytes() == request_bytes
    assert (tmp_path / 'receipt.json').read_bytes() == receipt_bytes
    second = _generate(tmp_path, parts, output=tmp_path / 'request-2.json', receipt=tmp_path / 'receipt-2.json')
    assert second == first
    assert (tmp_path / 'request-2.json').read_bytes() == request_bytes
    assert (tmp_path / 'receipt-2.json').read_bytes() == receipt_bytes
    (tmp_path / 'request.json').write_bytes(b'{}\n')
    (tmp_path / 'request.json').chmod(0o600)
    with pytest.raises(ValueError, match='disagrees'):
        _generate(tmp_path, parts)
    (tmp_path / 'request.json').write_bytes(request_bytes)
    (tmp_path / 'receipt.json').write_bytes(b'{}\n')
    (tmp_path / 'receipt.json').chmod(0o600)
    with pytest.raises(ValueError, match='disagrees'):
        _generate(tmp_path, parts)


def _cli(root, catalog, files, wiki, snapshot):
    env = {'PATH': os.environ['PATH'], 'PYTHONPATH': str(Path(__file__).parents[1])}
    return subprocess.run([sys.executable, '-m', 'knowledge_platform.distribution.claw_migration_request',
                           '--snapshot-root', str(snapshot), '--catalog', str(catalog),
                           '--files-root', str(files), '--wiki-root', str(wiki),
                           '--installation-id', 'install-1', '--source-revision', 'legacy-1',
                           '--source-schema-revision', 'legacy-v1',
                           '--map', LEGACY + '=external/knowledge',
                           '--output', str(root / 'cli-request.json'), '--receipt', str(root / 'cli-receipt.json')],
                          env=env, cwd=root, capture_output=True, text=True)


def test_cli_generates_request_and_receipt(tmp_path):
    snapshot, catalog, files, wiki = _snapshot(tmp_path)
    completed = _cli(tmp_path, catalog, files, wiki, snapshot)
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result['format'] == FORMAT and result['counts'] == {'documents': 3, 'originals': 1, 'attachments': 3, 'bytes': result['counts']['bytes']}
    request = json.loads((tmp_path / 'cli-request.json').read_text())
    assert request['format'] == REQUEST_FORMAT_V2
    assert stat.S_IMODE((tmp_path / 'cli-receipt.json').stat().st_mode) == 0o600


def test_cli_failure_is_redacted(tmp_path):
    snapshot, _, files, wiki = _snapshot(tmp_path)
    completed = _cli(tmp_path, tmp_path / 'missing.sqlite3', files, wiki, snapshot)
    assert completed.returncode == 1
    error = json.loads(completed.stdout)
    assert error['error_code'] == 'claw_migration_request_rejected' and error['activation_allowed'] is False
    assert str(tmp_path) not in completed.stdout + completed.stderr
