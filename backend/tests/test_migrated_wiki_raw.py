import asyncio
import base64
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient
from knowledge_contracts import Principal
from knowledge_platform.catalog.sqlite_query import SqliteCatalogQueryRepository
from knowledge_platform.local.app import _build_app
from knowledge_platform.local.workspace import open_persistent_workspace, WorkspaceError
from knowledge_platform.local.wiki import build_wiki_services
from knowledge_platform.local.wiki_query import PublishedWikiReader
from test_combined_workspace import _make
from test_knowledge_platform_local_wiki_processing import _install_fake_model


def test_owned_raw_read_compile_query_and_restart(tmp_path, monkeypatch):
    _install_fake_model(monkeypatch)
    document, wiki, _ = _make(tmp_path)
    state = tmp_path / 'state'
    with open_persistent_workspace(state, document_migration=document, wiki_archive=wiki) as owned:
        raw_id, raw_path = next(iter(owned['raw_bindings'].items()))
        assert raw_path.read_bytes() == b'history'
        assert raw_id not in owned['wiki_bindings'] and raw_id not in owned['document_bindings']
        with sqlite3.connect(owned['catalog']) as db:
            space, revision = db.execute('SELECT space_id,revision FROM knowledge_assets WHERE id=?', (raw_id,)).fetchone()
    document.rename(tmp_path / 'document-offline')
    wiki.rename(tmp_path / 'archive-offline')
    config = {'version': 2, 'space_id': space, 'assets': {raw_id: str(raw_path)}, 'model': {'endpoint': 'http://127.0.0.1:9999/v1/chat/completions', 'model': 'fixture'}}
    previous = None
    for _ in range(2):
        with open_persistent_workspace(state) as owned:
            services = build_wiki_services(config, owned['catalog'], state / 'processing/wiki')
            reader = PublishedWikiReader(SqliteCatalogQueryRepository(owned['catalog']), services)
            principal = Principal(subject_id='test', scopes=('knowledge.query', 'knowledge.read', 'knowledge.search', 'knowledge.processing', f'knowledge.space:{space}'))
            app = _build_app(SqliteCatalogQueryRepository(owned['catalog']), owned['file_bindings'], principal, wiki_compilation=services.wiki_compilation, wiki_provider=reader, wiki_blob_reader=reader, document_bindings=owned['document_bindings'])
            with TestClient(app) as client:
                response = client.post(f'/v1/assets/{raw_id}:read', json={'start': 0, 'end': 4096})
                assert base64.b64decode(response.json()['data']['content_base64']) == b'history'
                response = client.post('/v1/query', json={'query': 'history', 'space_id': space})
                assert response.json()['status'] == 'ok', response.text
                assert raw_id not in response.text, response.text
                response = client.post(f'/v1/wiki/assets/{raw_id}:compile', json={'snapshot_id': raw_id, 'source_revision': revision, 'source_uri': f'knowledge://spaces/{space}/assets/{raw_id}', 'content_digest': revision, 'idempotency_key': 'raw-once'})
                assert response.json()['status'] == 'ok', response.text
                result = response.json()['data']['compilation']
                if previous is not None: assert result == previous
                previous = result
                with sqlite3.connect(owned['catalog']) as db:
                    cid = db.execute("SELECT id FROM knowledge_datasets WHERE id LIKE 'collection_compiled_wiki_%'").fetchone()[0]
                response = client.post('/v1/query', json={'query': 'history', 'space_id': space, 'collection_id': cid})
                assert result['resource_uri'] in response.text, response.text


@pytest.mark.parametrize('field', ['content_digest', 'source_uri', 'metadata'])
def test_raw_catalog_and_manifest_cannot_jointly_forge_archive(tmp_path, field):
    document, wiki, _ = _make(tmp_path)
    state = tmp_path / 'state'
    with open_persistent_workspace(state, document_migration=document, wiki_archive=wiki) as owned:
        raw_id = next(iter(owned['raw_bindings']))
    path = state / 'workspace.json'
    manifest = json.loads(path.read_text())
    value = {'snapshot_path': 'another.md'} if field == 'metadata' else 'forged'
    manifest['wiki_manifest']['facts']['assets'][raw_id][field] = value
    column = 'metadata_json' if field == 'metadata' else field
    with sqlite3.connect(state / 'catalog.sqlite3') as db:
        db.execute(f'UPDATE knowledge_assets SET {column}=? WHERE id=?', (json.dumps(value) if field == 'metadata' else value, raw_id))
    path.write_text(json.dumps(manifest))
    with pytest.raises(WorkspaceError): open_persistent_workspace(state)


def test_previous_v3_workspace_still_opens(tmp_path):
    _, wiki, _ = _make(tmp_path)
    state = tmp_path / 'state'
    with open_persistent_workspace(state, wiki_archive=wiki) as owned:
        raw_ids = set(owned['raw_bindings'])
    path = state / 'workspace.json'
    manifest = json.loads(path.read_text())
    manifest['version'] = 3
    for raw_id in raw_ids:
        manifest['file_bindings'].pop(raw_id)
        manifest['facts']['assets'].pop(raw_id)
    with sqlite3.connect(state / 'catalog.sqlite3') as db:
        db.execute("DELETE FROM knowledge_assets WHERE source_type='local_wiki_raw'")
    path.write_text(json.dumps(manifest))
    with open_persistent_workspace(state) as owned:
        assert not owned['raw_bindings'] and owned['wiki_bindings']


def test_raw_only_workspace_initializes_without_published_pages(tmp_path):
    import hashlib
    from knowledge_platform.distribution.wiki_archive import prepare_wiki_archive
    brain = tmp_path / 'brain'
    (brain / 'raw').mkdir(parents=True)
    (brain / 'wiki').mkdir()
    (brain / 'raw/a.md').write_bytes(b'uncompiled raw')
    (brain / 'raw/manifest.jsonl').write_text(json.dumps({'snapshot_path': 'a.md', 'sha256': hashlib.sha256(b'uncompiled raw').hexdigest(), 'size_bytes': 14}) + '\n')
    archive = tmp_path / 'archive'
    prepare_wiki_archive(brain, archive)
    with open_persistent_workspace(tmp_path / 'state', wiki_archive=archive) as owned:
        assert owned['pages'] == 0 and not owned['wiki_bindings']
        assert len(owned['raw_bindings']) == 1
    with open_persistent_workspace(tmp_path / 'state') as owned:
        assert owned['raw_bindings']
