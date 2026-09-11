import asyncio
import os
import hashlib
import json
import sqlite3

import pytest
from test_document_migration import _run
from knowledge_platform.distribution.document_migration import prepare_document_migration


def test_retry_verifies_source_semantics_not_only_manifest(tmp_path):
    _,catalog,files,output,body=_run(tmp_path)
    assert prepare_document_migration(catalog,files,{'doc-1':'docs/readme.md'},output,installation_id='install-1',source_revision='legacy-1')['idempotent']
    with sqlite3.connect(output/'catalog.sqlite3') as db:
        db.execute("UPDATE knowledge_assets SET title='forged title'")
    manifest=json.loads((output/'manifest.json').read_text())
    manifest['files']['catalog.sqlite3']='sha256:'+hashlib.sha256((output/'catalog.sqlite3').read_bytes()).hexdigest()
    (output/'manifest.json').write_text(json.dumps(manifest))
    from knowledge_platform.catalog.rehearsal import RehearsalVerificationError
    with pytest.raises(RehearsalVerificationError):prepare_document_migration(catalog,files,{'doc-1':'docs/readme.md'},output,installation_id='install-1',source_revision='legacy-1')


def test_blob_and_manifest_forgery_rejected(tmp_path):
    _,catalog,files,output,body=_run(tmp_path)
    manifest=json.loads((output/'manifest.json').read_text());relative=next(iter(manifest['asset_bindings'].values()))
    (output/relative).write_bytes(b'forged')
    manifest['files'][relative]='sha256:'+hashlib.sha256(b'forged').hexdigest();(output/'manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(ValueError):prepare_document_migration(catalog,files,{'doc-1':'docs/readme.md'},output,installation_id='install-1',source_revision='legacy-1')


def test_target_retrieval_survives_source_removal(tmp_path):
    from knowledge_platform.catalog.sqlite_query import SqliteCatalogQueryRepository
    from knowledge_platform.retrieval.local import LocalDocumentRetrievalProvider
    _,catalog,files,output,body=_run(tmp_path)
    manifest=json.loads((output/'manifest.json').read_text())
    catalog.rename(tmp_path/'legacy-offline')
    files.rename(tmp_path/'files-offline')
    provider=LocalDocumentRetrievalProvider(catalog=SqliteCatalogQueryRepository(output/'catalog.sqlite3'),asset_paths={key:output/value for key,value in manifest['asset_bindings'].items()})
    results=asyncio.run(provider.search(query='portable content',space_id='space_kb-1',limit=5))
    assert len(results)==1
    assert 'portable content' in results[0].quote


@pytest.mark.parametrize('name',['manifest.json','catalog.sqlite3','blobs'])
def test_private_output_modes_cannot_be_weakened_on_retry(tmp_path,name):
    _,catalog,files,output,body=_run(tmp_path)
    (output/name).chmod(0o755 if name=='blobs' else 0o644)
    with pytest.raises(ValueError,match='permissions'):
        prepare_document_migration(catalog,files,{'doc-1':'docs/readme.md'},output,installation_id='install-1',source_revision='legacy-1')


def test_catalog_hardlink_rejected(tmp_path):
    _,catalog,files,output,body=_run(tmp_path)
    os.link(catalog,tmp_path/'alias.sqlite3')
    with pytest.raises(ValueError):
        prepare_document_migration(catalog,files,{'doc-1':'docs/readme.md'},tmp_path/'other',installation_id='install-1',source_revision='legacy-1')
