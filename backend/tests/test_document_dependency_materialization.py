import hashlib
import json
from pathlib import Path
import sqlite3
from urllib.parse import unquote, urlsplit

import pytest
from test_document_reverse import setup, run


def relative_fixture(tmp_path):
    args=list(setup(tmp_path));root=args[3]
    (root/'docs').mkdir();(root/'images').mkdir();(root/'files').mkdir()
    markdown=b'# Body\n\n![Picture](../images/pic%23x.png)\n\n[More](other.md)\n'
    (root/'docs/main.md').write_bytes(markdown)
    (root/'images/pic#x.png').write_bytes(b'picture fixture')
    (root/'docs/other.md').write_text('[CSV](../files/data.csv)\n')
    (root/'files/data.csv').write_text('name,count\nexample,1\n')
    asset=next(iter(args[4]));args[4]={asset:'docs/main.md'}
    with sqlite3.connect(args[2]) as db:
        digest='sha256:'+hashlib.sha256(markdown).hexdigest()
        db.execute('UPDATE knowledge_assets SET content_digest=?,revision=?',(digest,digest))
    # Real old generated metadata used by _document_has_llamaindex_chunks and
    # local vector refresh reporting; it cannot survive a path/body change.
    for index,path in enumerate(args[:3]):
        table,column=('knowledge_documents','doc_metadata') if index==0 else ('knowledge_assets','metadata_json')
        with sqlite3.connect(path) as db:
            metadata=json.loads(db.execute('SELECT '+column+' FROM '+table).fetchone()[0])
            metadata.update({'llamaindex_chunks':{'chunks':[{'file_path':'/old/body.md'}],'chunk_count':1},
                             'vector_index':{'refreshed':True},'markdown_sha256':'stale-body-hash'})
            if index:
                from knowledge_platform.catalog.rehearsal_runner import _redact
                metadata = _redact(metadata)
            db.execute('UPDATE '+table+' SET '+column+'=?',(json.dumps(metadata),))
    return args


def test_relative_image_and_transitive_attachment_read_from_candidate(tmp_path):
    args=relative_fixture(tmp_path);source_bytes=[p.read_bytes() for p in args[:3]];receipt=run(args)
    with sqlite3.connect(args[-1]/'catalog.sqlite3') as db:
        raw_path,raw_metadata=db.execute('SELECT storage_path,doc_metadata FROM knowledge_documents').fetchone()
    path=Path(raw_path);metadata=json.loads(raw_metadata)
    image=path.parent/unquote(urlsplit('../images/pic%23x.png').path)
    assert image.read_bytes()==b'picture fixture'
    assert (path.parent/'other.md').read_text()=='[CSV](../files/data.csv)\n'
    assert (path.parent/'../files/data.csv').read_text()=='name,count\nexample,1\n'
    assert metadata['markdown_sha256']==hashlib.sha256(path.read_bytes()).hexdigest()
    assert 'llamaindex_chunks' not in metadata and 'vector_index' not in metadata
    assert metadata['nested']['token']=='private-value'
    assert receipt['dependency_file_count']==4 and receipt['relative_dependencies_materialized']
    assert not receipt['indexes_rebuilt']
    invalidated=next(iter(receipt['derived_metadata_invalidation'].values()))['fields']
    assert invalidated['llamaindex_chunks']['action']=='removed_for_rebuild'
    assert '/old/body.md' not in json.dumps(receipt)
    assert source_bytes==[p.read_bytes() for p in args[:3]]
    assert run(args)==dict(receipt,idempotent=True)


def test_missing_completed_dependency_rejects_without_repair(tmp_path):
    args=relative_fixture(tmp_path);run(args);target=args[-1]/'bodies/images/pic#x.png';target.unlink()
    manifest=(args[-1]/'manifest.json').read_bytes()
    with pytest.raises(ValueError):run(args)
    assert not target.exists() and (args[-1]/'manifest.json').read_bytes()==manifest


def test_dependency_mutation_during_copy_cannot_complete(tmp_path):
    args=relative_fixture(tmp_path)
    def mutate(name):
        if name=='docs/main.md':(args[3]/'images/pic#x.png').write_bytes(b'mutated')
    with pytest.raises(ValueError):run(args,_after_copy=mutate)
    assert json.loads((args[-1]/'manifest.json').read_text())['state']=='copying'


@pytest.mark.parametrize('filename',['raw-body','raw-body.bin'])
def test_extensionless_markdown_blob_retains_readable_alias_and_dependencies(tmp_path,filename):
    args=list(setup(tmp_path));root=args[3];(root/'blobs').mkdir()
    body=b'![picture](image.png)\n';(root/'blobs'/filename).write_bytes(body);(root/'blobs/image.png').write_bytes(b'image')
    args[4]={next(iter(args[4])):'blobs/'+filename}
    with sqlite3.connect(args[2]) as db:
        digest='sha256:'+hashlib.sha256(body).hexdigest();db.execute('UPDATE knowledge_assets SET content_digest=?,revision=?',(digest,digest))
    run(args)
    with sqlite3.connect(args[-1]/'catalog.sqlite3') as db:path=Path(db.execute('SELECT storage_path FROM knowledge_documents').fetchone()[0])
    assert path.name==filename+'.md' and path.read_bytes()==body
    assert (path.parent/'image.png').read_bytes()==b'image'
