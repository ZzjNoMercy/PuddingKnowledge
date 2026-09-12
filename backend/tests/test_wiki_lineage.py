import hashlib
import json
from pathlib import Path

import pytest
from knowledge_platform.distribution.wiki_archive import prepare_wiki_archive, verify_archive
from knowledge_platform.local.wiki_raw import project_raw_assets
from knowledge_platform.local.wiki_lineage import project_wiki_lineage

DIGEST = hashlib.sha256(b'raw').hexdigest()

def publish(job='wiki-first', at='2026-09-12T00:00:00Z', **updates):
    return {'job_id':job,'status':'published','published_at':at,'raw_hashes':{'a.md':DIGEST},'consumed_raw_by_page':{'page':['a.md']},**updates}

def retire(job='wiki-retire-second', at='2026-09-12T00:00:01Z', **updates):
    return {'job_id':job,'status':'published','operation':'page-retirement','published_at':at,'raw_hashes':{'a.md':DIGEST},'retired_pages':{'page':'replacement'},'archive_dir':f'.puddingclaw/retired/{job}/wiki',**updates}

def fixture(root, receipts, *, active=('page',), archives=True, edit=None):
    source=root/'source';(source/'raw').mkdir(parents=True);(source/'wiki').mkdir()
    (source/'raw/a.md').write_bytes(b'raw')
    (source/'raw/manifest.jsonl').write_text(json.dumps({'snapshot_path':'a.md','sha256':DIGEST,'size_bytes':3})+'\n')
    for slug in active:
        path=source/'wiki'/f'{slug}.md';path.parent.mkdir(parents=True,exist_ok=True);path.write_text('page')
    jobs=source/'.puddingclaw/jobs';jobs.mkdir(parents=True)
    for receipt in receipts:
        (jobs/(receipt['job_id']+'.json')).write_text(json.dumps(receipt))
        if archives and receipt.get('operation')=='page-retirement':
            for slug in receipt['retired_pages']:
                path=source/f".puddingclaw/retired/{receipt['job_id']}/wiki/{slug}.md";path.parent.mkdir(parents=True,exist_ok=True);path.write_text('old')
    if edit:edit(source)
    archive=root/'archive';prepare_wiki_archive(source,archive)
    manifest=verify_archive(archive)
    raw=project_raw_assets(archive,manifest,'space_test')['assets']
    return archive,manifest,raw

def project(root, receipts, **kwargs):
    return next(iter(project_wiki_lineage(*fixture(root,receipts,**kwargs)).values()))

def test_only_consumed_paths_confer_consumption(tmp_path):
    selected=project(tmp_path/'selected',[publish(consumed_raw_by_page={})])
    consumed=project(tmp_path/'consumed',[publish()])
    assert not selected['historical_consumed'] and selected['historical_receipt_digests']==[]
    assert consumed['historical_consumed'] and consumed['historical_compiled_pages']==['page']

def test_retirement_keeps_consumption_and_evidence(tmp_path):
    receipts=[publish(),retire()]
    result=project(tmp_path,receipts,active=('replacement',))
    assert result['historical_consumed'] and result['historical_compiled_pages']==[]
    assert result['historical_job_ids']==['wiki-first']
    expected={hashlib.sha256(json.dumps(r).encode()).hexdigest() for r in receipts}
    assert set(result['historical_receipt_digests'])==expected
    event=result['historical_retirements'][0]
    assert event['slug']=='page' and event['replacement']=='replacement' and event['receipt_digest'] in expected

def test_republish_and_fractional_timestamp_order(tmp_path):
    result=project(tmp_path,[publish(),retire(at='2026-09-12T08:00:00.1+08:00'),publish('wiki-third','2026-09-12T00:00:00.2Z')])
    assert result['historical_compiled_pages']==['page']
    assert result['historical_job_ids']==['wiki-first','wiki-third']
    assert len(result['historical_retirements'])==1

def test_retired_active_page_conflict(tmp_path):
    with pytest.raises(ValueError,match='remains active'):project(tmp_path,[publish(),retire()])

def test_missing_retired_archive(tmp_path):
    with pytest.raises(ValueError,match='Missing retired'):project(tmp_path,[publish(),retire()],active=(),archives=False)

@pytest.mark.parametrize('updates',[
    {'raw_hashes':{'a.md':'0'*64}},
    {'consumed_raw_by_page':{'page':['missing.md']}},
    {'consumed_raw_by_page':{'page':[{}]}},
    {'consumed_raw_by_page':{'../escape':['a.md']}},
    {'consumed_raw_by_page':{'page':['a.md','a.md']}},
    {'published_at':'yesterday'},
    {'published_at':'2026-09-12T00:00:00'},
    {'operation':{}},
    {'operation':'unknown'},
    {'retired_pages':[]},
])
def test_invalid_published_semantics_reject(tmp_path,updates):
    with pytest.raises(ValueError):project(tmp_path,[publish(**updates)])

def test_duplicate_json_keys_reject(tmp_path):
    def edit(source):
        path=source/'.puddingclaw/jobs/wiki-first.json'
        path.write_text(path.read_text()[:-1]+',"status":"published"}')
    with pytest.raises(ValueError):project(tmp_path,[publish()],edit=edit)

def test_filename_job_identity_reject(tmp_path):
    def edit(source):(source/'.puddingclaw/jobs/wiki-first.json').rename(source/'.puddingclaw/jobs/wiki-other.json')
    with pytest.raises(ValueError,match='job_id'):project(tmp_path,[publish()],edit=edit)

def test_nonpublished_receipt_does_not_confer_consumption(tmp_path):
    result=project(tmp_path,[publish(status='failed')])
    assert not result['historical_consumed']

def test_workspace_prefix_migration_preserves_consumption(tmp_path):
    first=publish(consumed_raw_by_page={'wiki/page':['a.md']})
    migration=publish('wiki-migrate-next','2026-09-12T00:00:01Z',operation='workspace-prefix-migration',moved={'wiki/page':'page'})
    result=project(tmp_path,[first,migration])
    assert result['historical_compiled_pages']==['page']
    assert len(result['historical_receipt_digests'])==2

def test_nested_index_is_a_page(tmp_path):
    result=project(tmp_path,[publish(consumed_raw_by_page={'concept/index':['a.md']})],active=('concept/index',))
    assert result['historical_compiled_pages']==['concept/index']

def test_changed_receipt_bytes_rejected_against_manifest(tmp_path):
    archive,manifest,raw=fixture(tmp_path,[publish()])
    (archive/'archive/.puddingclaw/jobs/wiki-first.json').write_text(json.dumps(publish(status='failed')))
    with pytest.raises(ValueError,match='inventory changed'):project_wiki_lineage(archive,manifest,raw)


def test_prefix_move_cannot_clear_retirement_without_publish(tmp_path):
    migration=publish('wiki-migrate-next','2026-09-12T00:00:02Z',operation='workspace-prefix-migration',moved={'other':'page'},consumed_raw_by_page={})
    with pytest.raises(ValueError):project(tmp_path,[publish(),retire(),migration])
