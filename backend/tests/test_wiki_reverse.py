import asyncio
import hashlib
import json
import os
from pathlib import Path
import sqlite3

import pytest

from knowledge_platform.distribution import wiki_archive as archive
from knowledge_platform.distribution.wiki_reverse import FORMAT, main, prepare_wiki_reverse
from knowledge_platform.distribution.wiki_schema_evidence import BRAIN, CUSTOM, capture_schema_evidence
from knowledge_platform.local.frozen_export import export_frozen_workspace
from knowledge_platform.local.workspace import open_persistent_workspace
from knowledge_platform.local.wiki import build_wiki_services
from knowledge_platform.local.wiki_authoring import WikiAuthoringStore
from knowledge_platform.local.writer_authority import enroll, suspend
from knowledge_platform.wiki.compiler import WikiCompilationRequest
from knowledge_platform.wiki.patch import PageChange, WikiPatch, digest
from knowledge_platform.wiki.ports import WikiDraft

INDEX = '# Index\n\n- [[concepts/base]]\n'
LOG = '# Log\n'
HISTORICAL = '2026-01-01T00:00:00+00:00'


def _page(title, body='A source-supported concept.'):
    return ('---\ntitle: ' + title + '\ntype: concept\nsources: [source.md]\ncreated: 2026-09-13\n'
            'updated: 2026-09-13\nschema_version: 0.1.0\n---\n# ' + title + '\n\n' + body)


BASE = _page('Base')
UPDATED = _page('Base', 'An updated source-supported concept.')
CONCEPT = _page('Concept')


def _brain(tmp_path):
    fixture = json.loads((Path(__file__).parent / 'fixtures/wiki-schema-legacy.json').read_text())
    brain = tmp_path / 'brain'
    for relative, key in [(CUSTOM, 'custom_yaml'), (BRAIN, 'brain_yaml'), ('AGENTS.md', 'agents_markdown')]:
        path = brain / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(fixture[key])
    (brain / 'wiki/concepts').mkdir(parents=True)
    (brain / 'wiki/index.md').write_text(INDEX)
    (brain / 'wiki/log.md').write_text(LOG)
    (brain / 'wiki/concepts/base.md').write_text(BASE)
    (brain / 'raw').mkdir()
    raw = b'The source describes a concept.'
    digest_hex = hashlib.sha256(raw).hexdigest()
    (brain / 'raw/source.md').write_bytes(raw)
    (brain / 'raw/manifest.jsonl').write_text(
        json.dumps({'snapshot_path': 'source.md', 'sha256': digest_hex, 'size_bytes': len(raw)}) + '\n')
    receipt = {'job_id': 'wiki-historical-1', 'status': 'published', 'published_at': '2026-01-01T00:00:00Z',
               'bundle_hash': fixture['expected_bundle_hash'], 'schema_id': 'puddingclaw-wiki',
               'schema_version': '0.1.0', 'raw_hashes': {'source.md': digest_hex}, 'pages': ['concepts/base'],
               'consumed_raw_by_page': {'concepts/base': ['source.md']}, 'lint': {'ok': True}}
    job = brain / '.puddingclaw/jobs/wiki-historical-1.json'
    job.parent.mkdir(parents=True)
    job.write_text(json.dumps(receipt, indent=2, sort_keys=True) + '\n')
    return brain, fixture, digest_hex


def _compile(state, owned, text, *, key='compile-one', schema=True):
    snapshot_id = next(iter(owned['raw_bindings']))
    space = owned['space_id']
    with sqlite3.connect(owned['catalog']) as db:
        content_digest = db.execute('SELECT content_digest FROM knowledge_assets WHERE id=?',
                                    (snapshot_id,)).fetchone()[0]
    request = WikiCompilationRequest(snapshot_id, content_digest,
                                     f'knowledge://spaces/{space}/assets/{snapshot_id}',
                                     content_digest, key)
    config = {'version': 2, 'space_id': space,
              'assets': {snapshot_id: str(owned['raw_bindings'][snapshot_id])},
              'model': {'endpoint': 'http://127.0.0.1:1', 'model': 'fixture'}}
    services = build_wiki_services(config, owned['catalog'], state / 'processing',
                                   schema_workspace=owned if schema else None)

    class Model:
        async def generate(self, *, context, snapshot):
            return WikiDraft(path='wiki/concepts/new.md', title='Concept', markdown=text,
                             source_snapshot_id=snapshot.snapshot_id, source_revision=snapshot.source_revision)

    services.wiki_compilation._model = Model()
    return asyncio.run(services.wiki_compilation.compile(request))


def _author(owned, patches):
    store = WikiAuthoringStore.from_owned_workspace(owned)
    revision = store.read()[0]
    for number, (changes, index, log_entry) in enumerate(patches):
        patch = WikiPatch(revision, tuple(changes), ('source.md',), index, log_entry)
        revision = store.apply(patch, operation_id=f'authoring-{number}')


def _pipeline(tmp_path, ops=None):
    brain, fixture, digest_hex = _brain(tmp_path)
    candidate = tmp_path / 'archive'
    archive.prepare_wiki_archive(brain, candidate)
    files = {}
    for name, text in fixture['catalog_yaml'].items():
        path = tmp_path / (name + '.yaml')
        path.write_text(text)
        files[name] = path
    evidence = tmp_path / 'schema.json'
    evidence.write_bytes(capture_schema_evidence(candidate, files, expected_bundle_hash=fixture['expected_bundle_hash']))
    evidence.chmod(0o600)
    state = tmp_path / 'state'
    with open_persistent_workspace(state, wiki_archive=candidate, schema_evidence=evidence) as owned:
        pass
    if ops is not None:
        ops(state, owned)
    authority = tmp_path / 'authority'
    enroll(state, authority, 'enrollment-1')
    suspend(state, 'freeze-1')
    export = tmp_path / 'export'
    export_frozen_workspace(state, export, 'freeze-1')
    return brain, candidate, export, state, fixture


def _reverse(brain, candidate, export, out, **kwargs):
    return prepare_wiki_reverse(brain, candidate, export, out, **kwargs)


def _tree(root):
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob('*') if path.is_file()}


def _jobs(root):
    jobs = {}
    base = root / '.puddingclaw/jobs'
    if base.is_dir():
        for path in base.iterdir():
            jobs[path.name] = json.loads(path.read_text())
    return jobs


def test_pure_roundtrip_restores_byte_identical_brain_and_is_idempotent(tmp_path):
    brain, candidate, export, _state, _fixture = _pipeline(tmp_path)
    out = tmp_path / 'reverse'
    receipt = _reverse(brain, candidate, export, out)
    assert receipt['format'] == FORMAT and receipt['state'] == 'verified_inactive_wiki'
    assert receipt['delta'] == {'pages_added': [], 'pages_updated': [], 'pages_retired': [],
                                'compilations_reversed': 0, 'authoring_commits_reversed': 0,
                                'receipts_written': 0, 'retired_pages_archived': 0}
    assert receipt['receipts_written'] == 0 and receipt['job_ids'] == [] and not receipt['idempotent']
    assert receipt['wiki_domain_reversed'] and receipt['raw_domain_unchanged'] and receipt['schema_unchanged']
    for flag in ('activation_allowed', 'rollback_completed', 'credential_continuity_verified',
                 'indexes_rebuilt', 'installation_path_rebound'):
        assert receipt[flag] is False
    assert _tree(out / 'brain') == _tree(brain)
    manifest = json.loads((out / 'manifest.json').read_text())
    assert manifest['state'] == 'verified_inactive_wiki' and manifest['activation_allowed'] is False
    assert _reverse(brain, candidate, export, out) == dict(receipt, idempotent=True)
    assert _tree(out / 'brain') == _tree(brain)


def test_schema_compilation_is_reversed_to_legacy_page_and_receipt(tmp_path):
    brain, candidate, export, state, fixture = _pipeline(
        tmp_path, lambda state, owned: _compile(state, owned, CONCEPT))
    out = tmp_path / 'reverse'
    receipt = _reverse(brain, candidate, export, out)
    assert receipt['delta']['pages_added'] == ['concepts/new'] and receipt['delta']['compilations_reversed'] == 1
    assert receipt['receipts_written'] == 1
    tree = _tree(out / 'brain')
    source = _tree(brain)
    jobs = _jobs(out / 'brain')
    assert len(jobs) == 2  # the historical receipt is preserved byte-identically
    record = next(record for name, record in jobs.items() if name != 'wiki-historical-1.json')
    assert record['status'] == 'published' and 'operation' not in record
    assert record['job_id'].startswith('wiki-reverse-')
    assert record['pages'] == ['concepts/new'] and record['consumed_raw_by_page'] == {'concepts/new': ['source.md']}
    assert record['bundle_hash'] == fixture['expected_bundle_hash']
    assert record['schema_id'] == 'puddingclaw-wiki' and record['schema_version'] == '0.1.0'
    assert record['reversed'] is True and record['reverse']['origin'] == 'knowledge_local_wiki_compilations'
    assert record['reverse']['synthetic_published_at'] is False
    with sqlite3.connect(state / 'catalog.sqlite3') as db:
        updated = db.execute("SELECT updated_at FROM knowledge_local_wiki_compilations WHERE status='succeeded'").fetchone()[0]
    assert record['published_at'] == updated
    name = record['job_id'] + '.json'
    assert set(tree) == set(source) | {'wiki/concepts/new.md', '.puddingclaw/jobs/' + name}
    assert tree['.puddingclaw/jobs/wiki-historical-1.json'] == source['.puddingclaw/jobs/wiki-historical-1.json']
    assert tree['wiki/concepts/new.md'] == (CONCEPT + '\n').encode()
    assert tree['wiki/index.md'] == (INDEX + '\n- [[concepts/new]]\n').encode()
    for relative, data in source.items():
        if relative != 'wiki/index.md':
            assert tree[relative] == data
    assert _reverse(brain, candidate, export, out)['idempotent'] is True


def test_authoring_update_replays_commit_chain_to_committed_state(tmp_path):
    def ops(state, owned):
        _author(owned, [([PageChange('concepts/base', UPDATED, digest(BASE))], INDEX + '\n', 'Update the base concept.')])

    brain, candidate, export, _state, _fixture = _pipeline(tmp_path, ops)
    out = tmp_path / 'reverse'
    receipt = _reverse(brain, candidate, export, out)
    assert receipt['delta']['pages_updated'] == ['concepts/base'] and receipt['delta']['authoring_commits_reversed'] == 1
    assert receipt['receipts_written'] == 1
    tree = _tree(out / 'brain')
    assert tree['wiki/concepts/base.md'] == (UPDATED + '\n').encode()
    assert tree['wiki/index.md'] == (INDEX + '\n').encode()
    assert tree['wiki/log.md'] == (LOG + 'Update the base concept.\n').encode()
    jobs = _jobs(out / 'brain')
    record = next(record for name, record in jobs.items() if name != 'wiki-historical-1.json')
    assert record['pages'] == ['concepts/base'] and record['published_at'] == HISTORICAL
    assert record['reverse']['origin'] == 'knowledge_wiki_authoring_commits'
    assert record['reverse']['synthetic_published_at'] is True
    assert set(record['reverse']['receipt_digest']) <= set('0123456789abcdef')
    source = _tree(brain)
    assert set(tree) == set(source) | {'.puddingclaw/jobs/' + record['job_id'] + '.json'}
    for relative in ('AGENTS.md', 'raw/source.md', 'raw/manifest.jsonl'):
        assert tree[relative] == source[relative]


def test_authoring_retirement_archives_before_image_and_removes_page(tmp_path):
    def ops(state, owned):
        _compile(state, owned, CONCEPT)
        _author(owned, [([PageChange('concepts/base', None, digest(BASE), 'concepts/new')],
                         '# Index\n\n- [[concepts/new]]\n', 'Retire the base concept.')])

    brain, candidate, export, _state, _fixture = _pipeline(tmp_path, ops)
    out = tmp_path / 'reverse'
    receipt = _reverse(brain, candidate, export, out)
    assert receipt['delta']['pages_added'] == ['concepts/new'] and receipt['delta']['pages_retired'] == ['concepts/base']
    assert receipt['delta']['retired_pages_archived'] == 1 and receipt['receipts_written'] == 2
    tree = _tree(out / 'brain')
    assert 'wiki/concepts/base.md' not in tree
    assert tree['wiki/concepts/new.md'] == (CONCEPT + '\n').encode()
    assert tree['wiki/log.md'] == (LOG + 'Retire the base concept.\n').encode()
    retirement = next(record for record in _jobs(out / 'brain').values() if record.get('operation') == 'page-retirement')
    assert retirement['job_id'].startswith('wiki-retire-reverse-')
    assert retirement['retired_pages'] == {'concepts/base': 'concepts/new'} and retirement['updated_pages'] == []
    assert retirement['archive_dir'] == '.puddingclaw/retired/' + retirement['job_id'] + '/wiki'
    assert retirement['retired_at'] == retirement['published_at']
    archived = out / 'brain' / retirement['archive_dir'] / 'concepts/base.md'
    assert archived.read_bytes() == (BASE + '\n').encode()
    assert retirement['reverse']['origin'] == 'knowledge_wiki_authoring_commits'


def test_multi_page_authoring_commit_writes_single_publish_receipt(tmp_path):
    one, two = _page('One'), _page('Two')
    index = '# Index\n\n- [[concepts/base]]\n- [[concepts/one]]\n- [[concepts/two]]\n'

    def ops(state, owned):
        _author(owned, [([PageChange('concepts/one', one, None), PageChange('concepts/two', two, None)],
                         index, 'Add two concepts.')])

    brain, candidate, export, _state, _fixture = _pipeline(tmp_path, ops)
    out = tmp_path / 'reverse'
    receipt = _reverse(brain, candidate, export, out)
    assert receipt['delta']['pages_added'] == ['concepts/one', 'concepts/two']
    assert receipt['receipts_written'] == 1 and receipt['delta']['authoring_commits_reversed'] == 1
    new_jobs = [record for name, record in _jobs(out / 'brain').items() if name != 'wiki-historical-1.json']
    assert len(new_jobs) == 1
    record = new_jobs[0]
    assert record['pages'] == ['concepts/one', 'concepts/two']
    assert record['consumed_raw_by_page'] == {'concepts/one': ['source.md'], 'concepts/two': ['source.md']}
    tree = _tree(out / 'brain')
    assert tree['wiki/concepts/one.md'] == (one + '\n').encode()
    assert tree['wiki/concepts/two.md'] == (two + '\n').encode()
    assert tree['wiki/index.md'] == index.encode()


def test_zero_commit_authoring_restores_archived_bytes_with_normalized_index(tmp_path):
    brain, candidate, export, _state, _fixture = _pipeline(
        tmp_path, lambda state, owned: WikiAuthoringStore.from_owned_workspace(owned))
    out = tmp_path / 'reverse'
    receipt = _reverse(brain, candidate, export, out)
    assert receipt['receipts_written'] == 0 and receipt['delta']['authoring_commits_reversed'] == 0
    tree = _tree(out / 'brain')
    source = _tree(brain)
    assert set(tree) == set(source)
    assert tree['wiki/index.md'] == (INDEX + '\n').encode()
    for relative, data in source.items():
        if relative != 'wiki/index.md':
            assert tree[relative] == data


def test_zero_commit_authoring_over_compilations_keeps_committed_pages(tmp_path):
    def ops(state, owned):
        _compile(state, owned, CONCEPT)
        WikiAuthoringStore.from_owned_workspace(owned)

    brain, candidate, export, _state, _fixture = _pipeline(tmp_path, ops)
    out = tmp_path / 'reverse'
    receipt = _reverse(brain, candidate, export, out)
    assert receipt['delta']['compilations_reversed'] == 1 and receipt['delta']['authoring_commits_reversed'] == 0
    assert receipt['receipts_written'] == 1
    tree = _tree(out / 'brain')
    assert tree['wiki/concepts/new.md'] == (CONCEPT + '\n').encode()
    assert tree['wiki/index.md'] == (INDEX + '\n- [[concepts/new]]\n').encode()
    assert tree['wiki/concepts/base.md'] == _tree(brain)['wiki/concepts/base.md']


@pytest.mark.parametrize('target', ['raw/source.md', 'wiki/index.md'])
def test_interruption_supports_exact_retry(tmp_path, target):
    brain, candidate, export, _state, _fixture = _pipeline(
        tmp_path, lambda state, owned: _compile(state, owned, CONCEPT))
    out = tmp_path / 'reverse'

    def fail(name):
        if name == target:
            raise RuntimeError('interrupted')

    with pytest.raises(RuntimeError):
        _reverse(brain, candidate, export, out, _after_copy=fail)
    assert json.loads((out / 'manifest.json').read_text())['state'] == 'copying'
    assert _reverse(brain, candidate, export, out)['state'] == 'verified_inactive_wiki'
    assert (out / 'brain/wiki/concepts/new.md').read_bytes() == (CONCEPT + '\n').encode()


def test_sigkill_releases_output_lock_and_resumes(tmp_path):
    import subprocess
    import sys
    import time

    brain, candidate, export, _state, _fixture = _pipeline(tmp_path)
    out = tmp_path / 'reverse'
    ready = tmp_path / 'ready'
    code = (
        'import time\nfrom pathlib import Path\n'
        'from knowledge_platform.distribution.wiki_reverse import prepare_wiki_reverse\n'
        'def pause(name):\n'
        '    if name == "raw/source.md":\n'
        '        Path(' + repr(str(ready)) + ').write_text(name)\n'
        '        while True: time.sleep(.02)\n'
        'prepare_wiki_reverse(' + ', '.join(repr(str(value)) for value in (brain, candidate, export, out))
        + ', _after_copy=pause)\n'
    )
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    child = subprocess.Popen([sys.executable, '-c', code], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 10
        while not ready.exists():
            if child.poll() is not None:
                raise AssertionError(child.stderr.read().decode())
            assert time.monotonic() < deadline
            time.sleep(.02)
        child.kill()
        child.wait(timeout=5)
        assert _reverse(brain, candidate, export, out)['state'] == 'verified_inactive_wiki'
        assert _tree(out / 'brain') == _tree(brain)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        child.stderr.close()


@pytest.mark.parametrize('mode', ['tamper-file', 'delete-file', 'stray-stage', 'stray-brain'])
def test_completed_output_is_not_repaired(tmp_path, mode):
    brain, candidate, export, _state, _fixture = _pipeline(tmp_path)
    out = tmp_path / 'reverse'
    _reverse(brain, candidate, export, out)
    manifest = (out / 'manifest.json').read_bytes()
    target = out / 'brain/wiki/log.md'
    if mode == 'tamper-file':
        target.write_bytes(b'changed')
    elif mode == 'delete-file':
        target.unlink()
    elif mode == 'stray-stage':
        (out / 'foreign').write_bytes(b'x')
    else:
        (out / 'brain/foreign.md').write_bytes(b'x')
    with pytest.raises((ValueError, OSError)):
        _reverse(brain, candidate, export, out)
    assert (out / 'manifest.json').read_bytes() == manifest
    if mode == 'tamper-file':
        assert target.read_bytes() == b'changed'
    elif mode == 'delete-file':
        assert not target.exists()


def test_source_drift_rejects_before_output(tmp_path):
    brain, candidate, export, _state, _fixture = _pipeline(tmp_path)
    (brain / 'wiki/log.md').write_text('# Forged log\n')
    with pytest.raises(ValueError):
        _reverse(brain, candidate, export, tmp_path / 'reverse')
    assert not (tmp_path / 'reverse').exists()


def test_baseline_archive_tamper_rejects(tmp_path):
    brain, candidate, export, _state, _fixture = _pipeline(tmp_path)
    (candidate / 'archive/wiki/log.md').write_text('# Forged log\n')
    with pytest.raises(ValueError):
        _reverse(brain, candidate, export, tmp_path / 'reverse')


def test_export_raw_tamper_rejects(tmp_path):
    brain, candidate, export, _state, _fixture = _pipeline(tmp_path)
    (export / 'raw/wiki-evidence/archive/wiki/log.md').write_text('# Forged log\n')
    with pytest.raises(ValueError):
        _reverse(brain, candidate, export, tmp_path / 'reverse')


def test_export_catalog_tamper_rejects(tmp_path):
    brain, candidate, export, _state, _fixture = _pipeline(tmp_path)
    with sqlite3.connect(export / 'normalized/catalog/catalog.sqlite3') as db:
        db.execute("UPDATE knowledge_spaces SET name='Forged'")
    with pytest.raises(ValueError):
        _reverse(brain, candidate, export, tmp_path / 'reverse')


@pytest.mark.parametrize('statement', [
    "UPDATE knowledge_assets SET title='Forged' WHERE source_type='local_published_wiki'",
    "UPDATE knowledge_assets SET permissions_json='{\"allow\":true}' WHERE source_type='local_published_wiki'",
    "INSERT INTO knowledge_datasets (id,space_id,name,version,kind,description,asset_ids,semantic_asset_ids,"
    "capabilities,freshness,permissions_json,manifest_digest,created_at,updated_at) "
    "SELECT 'dataset_foreign',space_id,name,version,kind,description,asset_ids,semantic_asset_ids,"
    "capabilities,freshness,permissions_json,manifest_digest,created_at,updated_at FROM knowledge_datasets",
])
def test_catalog_domain_drift_rejects(tmp_path, statement):
    def ops(state, owned):
        with sqlite3.connect(owned['catalog']) as db:
            db.execute(statement)

    brain, candidate, export, _state, _fixture = _pipeline(tmp_path, ops)
    with pytest.raises(ValueError):
        _reverse(brain, candidate, export, tmp_path / 'reverse')


def _rebind_export(export):
    catalog = export / 'normalized/catalog/catalog.sqlite3'
    data = catalog.read_bytes()
    fact = {'sha256': hashlib.sha256(data).hexdigest(), 'size_bytes': len(data)}
    marker = export / 'manifest.json'
    manifest = json.loads(marker.read_text())
    manifest['normalized_databases']['catalog']['normalized_catalog'] = {
        'digest': 'sha256:' + fact['sha256'], 'size': fact['size_bytes']}
    manifest['normalized_inventory']['files']['catalog/catalog.sqlite3'] = fact
    marker.write_text(json.dumps(manifest, sort_keys=True, separators=(',', ':'), ensure_ascii=False) + '\n')


def test_unknown_asset_source_rejects(tmp_path):
    brain, candidate, export, _state, _fixture = _pipeline(tmp_path)
    with sqlite3.connect(export / 'normalized/catalog/catalog.sqlite3') as db:
        db.execute("UPDATE knowledge_assets SET source_type='feishu_doc' WHERE source_type='local_wiki_raw'")
    _rebind_export(export)
    with pytest.raises(ValueError, match='Unknown Wiki domain Asset'):
        _reverse(brain, candidate, export, tmp_path / 'reverse')


def test_non_schema_compilation_rejects(tmp_path):
    brain, candidate, export, _state, _fixture = _pipeline(
        tmp_path, lambda state, owned: _compile(state, owned, '# Concept\n\nPlain compiled body.', schema=False))
    with pytest.raises(ValueError):
        _reverse(brain, candidate, export, tmp_path / 'reverse')


def test_replacement_free_retirement_rejects(tmp_path):
    def ops(state, owned):
        _author(owned, [([PageChange('concepts/base', None, digest(BASE), None)], '# Index\n', 'Retire the base concept.')])

    brain, candidate, export, _state, _fixture = _pipeline(tmp_path, ops)
    with pytest.raises(ValueError, match='replacement'):
        _reverse(brain, candidate, export, tmp_path / 'reverse')


def test_authoring_page_tamper_rejects(tmp_path):
    def ops(state, owned):
        _author(owned, [([PageChange('concepts/base', UPDATED, digest(BASE))], INDEX + '\n', 'Update the base concept.')])
        with sqlite3.connect(owned['catalog']) as db:
            db.execute("UPDATE knowledge_wiki_authoring_pages SET markdown='forged'")

    brain, candidate, export, _state, _fixture = _pipeline(tmp_path, ops)
    with pytest.raises(ValueError):
        _reverse(brain, candidate, export, tmp_path / 'reverse')


def test_unknown_compilation_status_rejects(tmp_path):
    def ops(state, owned):
        _compile(state, owned, CONCEPT)
        with sqlite3.connect(owned['catalog']) as db:
            db.execute("UPDATE knowledge_local_wiki_compilations SET status='failed'")

    brain, candidate, export, _state, _fixture = _pipeline(tmp_path, ops)
    with pytest.raises(ValueError, match='status'):
        _reverse(brain, candidate, export, tmp_path / 'reverse')


def test_unowned_output_rejects(tmp_path):
    brain, candidate, export, _state, _fixture = _pipeline(tmp_path)
    out = tmp_path / 'reverse'
    out.mkdir(mode=0o700)
    (out / 'foreign').write_bytes(b'x')
    with pytest.raises(ValueError, match='Unowned'):
        _reverse(brain, candidate, export, out)


def test_output_overlapping_input_rejects(tmp_path):
    brain, candidate, export, _state, _fixture = _pipeline(tmp_path)
    with pytest.raises(ValueError):
        _reverse(brain, candidate, export, brain / 'reverse')


def test_cli_success_and_rejection(tmp_path, capsys):
    brain, candidate, export, _state, _fixture = _pipeline(tmp_path)
    out = tmp_path / 'reverse'
    assert main(['--source-snapshot', str(brain), '--baseline-archive', str(candidate),
                 '--current-workspace', str(export), '--output', str(out)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['format'] == FORMAT and result['state'] == 'verified_inactive_wiki'
    assert main(['--source-snapshot', str(brain), '--baseline-archive', str(candidate),
                 '--current-workspace', str(brain), '--output', str(tmp_path / 'other')]) == 1
    error = json.loads(capsys.readouterr().out)
    assert error['error_code'] == 'wiki_reverse_rejected' and error['activation_allowed'] is False
