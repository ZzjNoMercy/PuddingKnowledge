import hashlib
import json
from pathlib import Path
import shutil
import sqlite3

import pytest
from sqlalchemy import create_engine, text
from knowledge_platform.distribution.document_migration import prepare_document_migration
from knowledge_platform.distribution.core_catalog_reverse import build_core_catalog_reverse, _rows, _projection, _metadata_overlay
from test_document_migration import _legacy_catalog


LEGACY_SCHEMA = Path(__file__).parent / "fixtures/legacy-core-reverse-schema.sql"


def fixture(tmp_path, *, real=False):
    source=tmp_path/'source.sqlite3';body=b'unchanged content'
    _legacy_catalog(source,content_digest=hashlib.sha256(body).hexdigest())
    with sqlite3.connect(source) as db:
        db.execute('ALTER TABLE knowledge_documents ADD COLUMN publish_targets JSON')
        db.execute('ALTER TABLE knowledge_documents ADD COLUMN size_bytes INTEGER')
        db.execute('UPDATE knowledge_documents SET doc_metadata=?, publish_targets=?, size_bytes=?',
                   (json.dumps({'nested':{'token':'private-value','label':'old'},'origin':'original'}),'["wiki"]',len(body)))
        db.executescript('CREATE TABLE unrelated(id TEXT PRIMARY KEY, value TEXT); INSERT INTO unrelated VALUES ("u1","keep-me");')
    if real:
        full=tmp_path/'full.sqlite3'
        with sqlite3.connect(source) as old, sqlite3.connect(full) as db:
            old.row_factory=sqlite3.Row
            db.executescript(LEGACY_SCHEMA.read_text())
            for table in ('knowledge_bases','knowledge_documents'):
                for original in old.execute('SELECT * FROM '+table):
                    row=dict(original)
                    db.execute('INSERT INTO '+table+' ('+','.join(row)+') VALUES ('+','.join('?' for _ in row)+')',tuple(row.values()))
        full.replace(source)
    files=tmp_path/'files';files.mkdir();(files/'body.md').write_bytes(body)
    package=tmp_path/'package'
    prepare_document_migration(source,files,{'doc-1':'body.md'},package,source_revision='legacy-1')
    before=package/'catalog.sqlite3';after=tmp_path/'after.sqlite3';shutil.copyfile(before,after)
    return source,before,after,tmp_path/'reverse.sqlite3'


def run(paths):
    return build_core_catalog_reverse(*paths,source_revision='legacy-1')


def facts(paths):
    return [hashlib.sha256(p.read_bytes()).hexdigest() for p in paths[:3]]


def test_title_metadata_and_secret_preservation_in_actual_old_schema(tmp_path):
    paths=fixture(tmp_path);source,before,after,out=paths
    with sqlite3.connect(after) as db:
        metadata=json.loads(db.execute('SELECT metadata_json FROM knowledge_assets').fetchone()[0])
        metadata['nested']['label']='new';metadata['legacy_status']='ready';metadata['legacy_virtual_path']='changed.md'
        db.execute('UPDATE knowledge_assets SET title=?, metadata_json=?',('new title',json.dumps(metadata)))
    original=facts(paths);receipt=run(paths)
    assert facts(paths)==original and not receipt['activation_allowed']
    with sqlite3.connect(out) as db:
        title,status,virtual,metadata,publish,size=db.execute('SELECT title,status,virtual_path,doc_metadata,publish_targets,size_bytes FROM knowledge_documents').fetchone()
        assert (title,status,virtual)==('new title','ready','changed.md')
        assert json.loads(metadata)['nested']=={'token':'private-value','label':'new'}
        assert publish=='["wiki"]' and size==len(b'unchanged content')
        assert db.execute('SELECT * FROM unrelated').fetchall()==[('u1','keep-me')]
    assert 'private-value' not in json.dumps(receipt)


def test_no_delta_preserves_all_legacy_row_values(tmp_path):
    paths=fixture(tmp_path);run(paths)
    with sqlite3.connect(paths[0]) as source,sqlite3.connect(paths[3]) as out:
        for table in ('knowledge_bases','knowledge_documents','unrelated'):
            assert source.execute('SELECT * FROM '+table).fetchall()==out.execute('SELECT * FROM '+table).fetchall()


def update_derived_datasets(after,legacy):
    from knowledge_platform.catalog.models import KnowledgeDataset
    rows=_projection(legacy,'legacy-1')['knowledge_datasets']
    engine=create_engine(f'sqlite:///{after}')
    try:
        with engine.begin() as db:
            db.execute(KnowledgeDataset.__table__.delete())
            if rows:db.execute(KnowledgeDataset.__table__.insert(),rows)
    finally:engine.dispose()


def test_new_space_and_updated_space_roundtrip(tmp_path):
    from knowledge_platform.catalog.models import KnowledgeSpace
    from knowledge_platform.catalog.rehearsal_runner import _canonical_space
    paths=fixture(tmp_path);legacy=_rows(paths[0],('knowledge_bases','knowledge_documents'))
    legacy['knowledge_bases'][0]['name']='Changed'
    new=dict(legacy['knowledge_bases'][0],id='kb-new',name='New space');legacy['knowledge_bases'].append(new)
    engine=create_engine(f'sqlite:///{paths[2]}')
    try:
        with engine.begin() as db:
            db.execute(KnowledgeSpace.__table__.update().values(name='Changed'))
            db.execute(KnowledgeSpace.__table__.insert().values(**_canonical_space(new)))
    finally:engine.dispose()
    update_derived_datasets(paths[2],legacy)
    result=run(paths)
    assert result['changes']['knowledge_bases']=={'deleted':0,'inserted':1,'updated':1}
    with sqlite3.connect(paths[3]) as db:
        assert db.execute('SELECT id,name FROM knowledge_bases ORDER BY id').fetchall()==[('kb-1','Changed'),('kb-new','New space')]


def remove_document(paths):
    legacy=_rows(paths[0],('knowledge_bases','knowledge_documents'));legacy['knowledge_documents']=[]
    with sqlite3.connect(paths[2]) as db:db.execute('DELETE FROM knowledge_assets')
    update_derived_datasets(paths[2],legacy)


def test_document_delete_without_dependents(tmp_path):
    paths=fixture(tmp_path);remove_document(paths)
    assert run(paths)['changes']['knowledge_documents']['deleted']==1
    with sqlite3.connect(paths[3]) as db:assert db.execute('SELECT * FROM knowledge_documents').fetchall()==[]


def test_delete_referenced_document_rejects_without_cascade_or_output(tmp_path):
    paths=fixture(tmp_path)
    with sqlite3.connect(paths[0]) as db:
        db.executescript('CREATE TABLE dependencies(id TEXT PRIMARY KEY, document_id TEXT REFERENCES knowledge_documents(id) ON DELETE CASCADE); INSERT INTO dependencies VALUES("dep","doc-1");')
    remove_document(paths);original=facts(paths)
    with pytest.raises(ValueError,match='orphan'):run(paths)
    assert not paths[3].exists() and facts(paths)==original


@pytest.mark.parametrize('change',[
    "UPDATE knowledge_assets SET content_digest='sha256:changed',revision='sha256:changed'",
    "UPDATE knowledge_assets SET permissions_json='{\"read\":true}'",
    "UPDATE knowledge_assets SET source_uri='knowledge://spoof'",
    "UPDATE knowledge_assets SET id='native-new-identity'",
    "UPDATE knowledge_assets SET description='not representable'",
    "UPDATE knowledge_datasets SET capabilities='[]'",
    "UPDATE knowledge_assets SET title='"+'x'*301+"'",
])
def test_unrepresented_delta_rejects(tmp_path,change):
    paths=fixture(tmp_path)
    with sqlite3.connect(paths[2]) as db:db.execute(change)
    original=facts(paths)
    with pytest.raises(ValueError):run(paths)
    assert not paths[3].exists() and facts(paths)==original


def test_changed_noncore_table_rejects(tmp_path):
    paths=fixture(tmp_path)
    for path in paths[1:3]:
        with sqlite3.connect(path) as db:db.executescript('CREATE TABLE other(id TEXT PRIMARY KEY,value TEXT); INSERT INTO other VALUES("one","before");')
    with sqlite3.connect(paths[2]) as db:db.execute('UPDATE other SET value="after"')
    with pytest.raises(ValueError,match='Unmapped'):run(paths)
    assert not paths[3].exists()


def test_changed_baseline_is_not_authority(tmp_path):
    paths=fixture(tmp_path)
    with sqlite3.connect(paths[1]) as db:db.execute('UPDATE knowledge_assets SET title="unrelated baseline"')
    with pytest.raises(ValueError,match='projection'):run(paths)


def test_changed_provenance_rejects(tmp_path):
    paths=fixture(tmp_path)
    with sqlite3.connect(paths[2]) as db:
        metadata=json.loads(db.execute('SELECT metadata_json FROM knowledge_assets').fetchone()[0]);metadata['legacy_document_id']='different'
        db.execute('UPDATE knowledge_assets SET metadata_json=?',(json.dumps(metadata),))
    with pytest.raises(ValueError,match='projection'):run(paths)


def test_redacted_nested_list_cannot_be_replaced():
    with pytest.raises(ValueError,match='redacted'):
        _metadata_overlay([{'token':'real','label':'old'}],[{'token':'<redacted>','label':'old'}],[{'token':'<redacted>','label':'new'}])


def test_existing_output_is_never_overwritten(tmp_path):
    paths=fixture(tmp_path);paths[3].write_bytes(b'existing')
    with pytest.raises(FileExistsError):run(paths)
    assert paths[3].read_bytes()==b'existing'


def test_actual_legacy_orm_schema_accepts_metadata_reverse(tmp_path):
    paths=fixture(tmp_path,real=True)
    with sqlite3.connect(paths[2]) as db:db.execute("UPDATE knowledge_assets SET title='current title'")
    receipt=run(paths)
    assert receipt['changes']['knowledge_documents']['updated']==1
    with sqlite3.connect(paths[3]) as db:
        assert db.execute('SELECT title,publish_targets,size_bytes,source_connection_id FROM knowledge_documents').fetchone()==('current title','["wiki"]',len(b'unchanged content'),None)
        assert len(db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall())==18


@pytest.mark.parametrize('change',['delete','insert','nested-delete','nested-insert'])
def test_redacted_metadata_key_addition_or_deletion_rejects_end_to_end(tmp_path,change):
    paths=fixture(tmp_path)
    with sqlite3.connect(paths[2]) as db:
        metadata=json.loads(db.execute('SELECT metadata_json FROM knowledge_assets').fetchone()[0])
        if change=='delete':del metadata['nested']
        elif change=='insert':metadata['new_token']='<redacted>'
        elif change=='nested-delete':del metadata['nested']['token']
        else:metadata['nested']['new_token']='<redacted>'
        db.execute('UPDATE knowledge_assets SET metadata_json=?',(json.dumps(metadata),))
    original=facts(paths)
    with pytest.raises(ValueError,match='redacted'):run(paths)
    assert not paths[3].exists() and facts(paths)==original


def test_unmodified_raw_json_and_timestamp_cells_are_byte_preserved(tmp_path):
    paths=fixture(tmp_path)
    # Formatting changes do not alter the proven semantic forward projection.
    with sqlite3.connect(paths[0]) as db:
        db.execute('UPDATE knowledge_documents SET publish_targets=?,doc_metadata=?,created_at=?',
                   ('[ "wiki" ]','{"nested":{"token":"private-value","label":"old"},"origin":"original"}','2026-09-11 00:00:00'))
    with sqlite3.connect(paths[2]) as db:db.execute('UPDATE knowledge_assets SET title="change only title"')
    run(paths)
    with sqlite3.connect(paths[0]) as source,sqlite3.connect(paths[3]) as out:
        query='SELECT publish_targets,doc_metadata,created_at FROM knowledge_documents'
        assert source.execute(query).fetchone()==out.execute(query).fetchone()
