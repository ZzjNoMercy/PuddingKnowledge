import json
import hashlib
import sqlite3

import pytest
from fastapi.testclient import TestClient
from knowledge_contracts import Principal
from knowledge_platform.catalog.sqlite_query import SqliteCatalogQueryRepository
from knowledge_platform.local.app import _build_app
from knowledge_platform.local.workspace import open_persistent_workspace, WorkspaceError
from knowledge_platform.distribution.wiki_archive import prepare_wiki_archive


def make(tmp_path, *, conflict=False):
    brain = tmp_path / 'brain'
    (brain / 'raw').mkdir(parents=True)
    (brain / 'wiki').mkdir()
    hashes = {name: hashlib.sha256(body).hexdigest() for name,body in [('a.md',b'consumed'),('b.md',b'selected')]}
    records = []
    for name,body in [('a.md',b'consumed'),('b.md',b'selected')]:
        (brain / 'raw' / name).write_bytes(body)
        records.append({'snapshot_path': name, 'sha256': hashes[name], 'size_bytes': len(body)})
    (brain / 'raw/manifest.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in records))
    (brain / 'wiki/replacement.md').write_text('# Replacement\n')
    jobs = brain / '.puddingclaw/jobs';jobs.mkdir(parents=True)
    publish = {'job_id':'wiki-first','status':'published','published_at':'2026-09-11T00:00:00Z','raw_hashes':hashes,'consumed_raw_by_page':{'old':['a.md']},'pages':['old']}
    retirement = {'job_id':'wiki-retire-second','status':'published','operation':'page-retirement','published_at':'2026-09-12T00:00:00Z','raw_hashes':hashes,'retired_pages':{'old':'replacement'},'archive_dir':'.puddingclaw/retired/wiki-retire-second/wiki'}
    archived = brain / retirement['archive_dir'];archived.mkdir(parents=True)
    (archived / 'old.md').write_text('# Old\n')
    for receipt in (publish,retirement):
        (jobs / (receipt['job_id']+'.json')).write_text(json.dumps(receipt))
    if conflict: (brain / 'wiki/old.md').write_text('# Old still active\n')
    archive = tmp_path / 'archive';prepare_wiki_archive(brain,archive)
    return archive


def test_consumed_retired_raw_remains_consumed_after_source_disconnection(tmp_path):
    archive = make(tmp_path)
    state = tmp_path / 'state'
    with open_persistent_workspace(state,wiki_archive=archive): pass
    archive.rename(tmp_path / 'archive-offline')
    (tmp_path / 'brain').rename(tmp_path / 'brain-offline')
    for _ in range(2):
        with open_persistent_workspace(state) as owned:
            repo = SqliteCatalogQueryRepository(owned['catalog'])
            principal = Principal(subject_id='test',scopes=('knowledge.list','knowledge.read','knowledge.search',f"knowledge.space:{owned['space_id']}"))
            with TestClient(_build_app(repo,owned['file_bindings'],principal)) as client:
                response = client.get('/v1/assets',params={'space_id':owned['space_id']})
                assert response.json()['status']=='ok',response.text
                raws = {r['title']:r for r in response.json()['data']['assets'] if r['kind']=='raw_snapshot'}
                consumed,selected = raws['a.md']['wiki_lineage'],raws['b.md']['wiki_lineage']
                assert consumed['historical_consumed'] is True
                assert consumed['historical_compiled_pages']==[]
                assert 'wiki-first' in consumed['historical_job_ids']
                assert len(consumed['historical_receipt_digests'])==2
                assert consumed['historical_retirements'][0]['slug']=='old'
                assert consumed['historical_retirements'][0]['replacement']=='replacement'
                assert selected['historical_consumed'] is False


def test_retired_page_remaining_active_rejects_initialization(tmp_path):
    archive = make(tmp_path,conflict=True)
    with pytest.raises((ValueError,RuntimeError)):
        open_persistent_workspace(tmp_path/'state',wiki_archive=archive)
    assert (tmp_path/'state/.initializing').exists()


def test_joint_manifest_catalog_lineage_forgery_rejected(tmp_path):
    state=tmp_path/'state'
    with open_persistent_workspace(state,wiki_archive=make(tmp_path)):pass
    path=state/'workspace.json';manifest=json.loads(path.read_text())
    raw_id=next(k for k,v in manifest['facts']['assets'].items() if v['kind']=='raw_snapshot' and v['metadata']['historical_consumed'])
    metadata=manifest['facts']['assets'][raw_id]['metadata'];metadata['historical_consumed']=False
    with sqlite3.connect(state/'catalog.sqlite3') as db:db.execute('UPDATE knowledge_assets SET metadata_json=? WHERE id=?',(json.dumps(metadata),raw_id))
    path.write_text(json.dumps(manifest))
    with pytest.raises(WorkspaceError):open_persistent_workspace(state)


def test_old_v5_workspace_without_lineage_remains_readable(tmp_path):
    state=tmp_path/'state'
    with open_persistent_workspace(state,wiki_archive=make(tmp_path)):pass
    path=state/'workspace.json';manifest=json.loads(path.read_text());manifest['version']=5
    with sqlite3.connect(state/'catalog.sqlite3') as db:
        for asset_id,fact in manifest['facts']['assets'].items():
            if fact['kind']!='raw_snapshot':continue
            metadata=fact['metadata']
            for field in list(metadata):
                if field.startswith('historical_'):metadata.pop(field)
            db.execute('UPDATE knowledge_assets SET metadata_json=? WHERE id=?',(json.dumps(metadata),asset_id))
    path.write_text(json.dumps(manifest))
    with open_persistent_workspace(state) as owned:assert owned['raw_bindings']


def test_public_lineage_does_not_expose_arbitrary_metadata(tmp_path):
    state=tmp_path/'state'
    with open_persistent_workspace(state,wiki_archive=make(tmp_path)) as owned:
        raw_id=next(iter(owned['raw_bindings']))
        with sqlite3.connect(state/'catalog.sqlite3') as db:
            metadata=json.loads(db.execute('SELECT metadata_json FROM knowledge_assets WHERE id=?',(raw_id,)).fetchone()[0])
            metadata.update({'source_path':'/private/sensitive/path','secret':'do-not-export'})
            db.execute('UPDATE knowledge_assets SET metadata_json=? WHERE id=?',(json.dumps(metadata),raw_id))
        repo=SqliteCatalogQueryRepository(state/'catalog.sqlite3')
        data=json.dumps(repo.get_asset(asset_id=raw_id))
        assert 'do-not-export' not in data and '/private/sensitive' not in data and 'wiki_lineage' in data
        metadata['historical_retirements']=[{'secret':'do-not-export'}]
        with sqlite3.connect(state/'catalog.sqlite3') as db:db.execute('UPDATE knowledge_assets SET metadata_json=? WHERE id=?',(json.dumps(metadata),raw_id))
        with pytest.raises(ValueError):repo.get_asset(asset_id=raw_id)
