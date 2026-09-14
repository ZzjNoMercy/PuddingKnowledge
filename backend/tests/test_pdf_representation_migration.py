import hashlib,json,sqlite3,shutil
from pathlib import Path
import pytest
from test_core_catalog_reverse import fixture
from knowledge_platform.distribution.document_migration import prepare_document_migration
from knowledge_platform.distribution.document_reverse import prepare_document_reverse
from knowledge_platform.local.workspace import open_persistent_workspace, WorkspaceError


def pdf_bytes():
    # A complete one-page PDF with a valid xref table; no parser/model service.
    objects=[b'<< /Type /Catalog /Pages 2 0 R >>',b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] >>']
    data=b'%PDF-1.4\n';offsets=[]
    for number,obj in enumerate(objects,1):
        offsets.append(len(data));data+=str(number).encode()+b' 0 obj\n'+obj+b'\nendobj\n'
    xref=len(data);data+=b'xref\n0 4\n0000000000 65535 f \n'
    for offset in offsets:data+=f'{offset:010d} 00000 n \n'.encode()
    return data+b'trailer\n<< /Size 4 /Root 1 0 R >>\nstartxref\n'+str(xref).encode()+b'\n%%EOF\n'


def pdf_fixture(tmp_path):
    source,_,_,_=fixture(tmp_path,real=True)
    files=tmp_path/'pdf-files';files.mkdir()
    original=pdf_bytes();body=b'# Parsed PDF\n\nIndependent body representation.\n'
    (files/'original.pdf').write_bytes(original);(files/'body.md').write_bytes(body)
    old='/old/imported/original.pdf'
    with sqlite3.connect(source) as db:
        metadata=json.loads(db.execute('SELECT doc_metadata FROM knowledge_documents').fetchone()[0])
        metadata.update(mode='multimodal_pdf',original_path=old,original_sha256=hashlib.sha256(original).hexdigest(),markdown_sha256=hashlib.sha256(body).hexdigest())
        db.execute('UPDATE knowledge_documents SET source_type=?,source_path=?,storage_path=?,mime_type=?,content_sha256=?,size_bytes=?,doc_metadata=?',('pdf_mineru',old,'/old/imported/body.md','text/markdown',hashlib.sha256(original).hexdigest(),len(body),json.dumps(metadata)))
    return source,files,tmp_path/'pdf-candidate',original,body


def migrate(args,**kwargs):
    source,files,candidate,*_=args
    return prepare_document_migration(source,files,{'doc-1':'body.md'},candidate,source_revision='legacy-1',original_bindings={'doc-1':'original.pdf'},**kwargs)


def test_real_pdf_body_original_bootstrap_restart_and_reverse(tmp_path):
    args=pdf_fixture(tmp_path);source,files,candidate,original,body=args;raw=source.read_bytes()
    migrate(args);assert migrate(args)['idempotent']
    manifest=json.loads((candidate/'manifest.json').read_text());asset=next(iter(manifest['asset_bindings']))
    assert (candidate/manifest['asset_bindings'][asset]).read_bytes()==body
    assert (candidate/manifest['plan']['original_bindings'][asset]).read_bytes()==original
    state=tmp_path/'state'
    with open_persistent_workspace(state,document_migration=candidate) as payload:
        assert payload['document_bindings'][asset].read_bytes()==body
    with open_persistent_workspace(state) as payload:assert payload['document_bindings'][asset].read_bytes()==body
    after=tmp_path/'after-pdf.sqlite3';shutil.copyfile(candidate/'catalog.sqlite3',after)
    out=tmp_path/'reverse-pdf'
    receipt=prepare_document_reverse(source,candidate/'catalog.sqlite3',after,state,manifest['asset_bindings'],out,source_revision='legacy-1',attachment_bindings={'/old/imported/original.pdf':manifest['plan']['original_bindings'][asset]})
    with sqlite3.connect(out/'catalog.sqlite3') as db:
        source_path,storage_path,digest,size,raw_metadata=db.execute('SELECT source_path,storage_path,content_sha256,size_bytes,doc_metadata FROM knowledge_documents').fetchone()
    metadata=json.loads(raw_metadata)
    assert Path(source_path).read_bytes()==original and source_path==metadata['original_path']
    assert Path(storage_path).read_bytes()==body and storage_path!=source_path
    assert digest==hashlib.sha256(original).hexdigest() and metadata['markdown_sha256']==hashlib.sha256(body).hexdigest() and size==len(body)
    assert metadata['nested']['token']=='private-value' and source.read_bytes()==raw
    assert not receipt['activation_allowed']


@pytest.mark.parametrize('mode',['missing-original','swapped-body','wrong-original','bad-claim','same-binding'])
def test_pdf_representation_confusion_rejects(tmp_path,mode):
    args=pdf_fixture(tmp_path);source,files,candidate,original,body=args
    bindings={'doc-1':'body.md'};originals={'doc-1':'original.pdf'}
    if mode=='missing-original':originals={}
    elif mode=='swapped-body':bindings['doc-1']='original.pdf'
    elif mode=='wrong-original':(files/'original.pdf').write_bytes(b'wrong')
    elif mode=='same-binding':originals['doc-1']='body.md'
    else:
        with sqlite3.connect(source) as db:
            meta=json.loads(db.execute('SELECT doc_metadata FROM knowledge_documents').fetchone()[0]);meta['markdown_sha256']='0'*64
            db.execute('UPDATE knowledge_documents SET doc_metadata=?',(json.dumps(meta),))
    with pytest.raises(ValueError):prepare_document_migration(source,files,bindings,candidate,original_bindings=originals)
    assert not (candidate/'manifest.json').exists()


def test_completed_original_missing_not_repaired(tmp_path):
    args=pdf_fixture(tmp_path);migrate(args);candidate=args[2]
    manifest=json.loads((candidate/'manifest.json').read_text());p=candidate/next(iter(manifest['plan']['original_bindings'].values()));p.unlink()
    with pytest.raises(ValueError):migrate(args)
    assert not p.exists()


def test_owned_original_missing_rejects_restart(tmp_path):
    args=pdf_fixture(tmp_path);migrate(args);state=tmp_path/'state'
    with open_persistent_workspace(state,document_migration=args[2]):pass
    owned=json.loads((state/'workspace.json').read_text());(state/next(iter(owned['original_bindings'].values()))).unlink()
    with pytest.raises(WorkspaceError):
        with open_persistent_workspace(state):pass


@pytest.mark.parametrize('new_native',[False,True])
def test_current_pdf_changes_and_new_native_pdf_reverse(tmp_path,new_native):
    from knowledge_platform.catalog.rehearsal_runner import _digest
    args=pdf_fixture(tmp_path);migrate(args);source,files,candidate,original,body=args
    before=candidate/'catalog.sqlite3';after=tmp_path/'after.sqlite3';shutil.copyfile(before,after)
    body2=b'# Revised parsing\n';original2=original+b'% revised original bytes\n'
    (files/'body.md').write_bytes(body2);(files/'original.pdf').write_bytes(original2)
    with sqlite3.connect(after) as db:
        db.row_factory=sqlite3.Row;row=dict(db.execute('SELECT * FROM knowledge_assets').fetchone())
        metadata=json.loads(row['metadata_json']);metadata.update(original_sha256=hashlib.sha256(original2).hexdigest(),markdown_sha256=hashlib.sha256(body2).hexdigest())
        if new_native:
            native='new-pdf-native'
            metadata={k:v for k,v in metadata.items() if not k.startswith('legacy_') and k not in ('source_reference_digest','origin_url_digest')}
            row.update(id=native,source_uri='knowledge://spaces/'+row['space_id']+'/assets/'+native)
        row.update(content_digest='sha256:'+hashlib.sha256(body2).hexdigest(),revision='sha256:'+hashlib.sha256(body2).hexdigest(),metadata_json=json.dumps(metadata))
        db.execute('DELETE FROM knowledge_assets')
        db.execute('INSERT INTO knowledge_assets ('+','.join(row)+') VALUES ('+','.join('?' for _ in row)+')',tuple(row.values()))
        dataset=dict(db.execute('SELECT * FROM knowledge_datasets').fetchone());ids=[row['id']]
        db.execute('UPDATE knowledge_datasets SET asset_ids=?,manifest_digest=?',(json.dumps(ids),_digest({'asset_ids':ids,'source_revision':'legacy-1','version':dataset['version']})))
    out=tmp_path/'reverse-new'
    prepare_document_reverse(source,before,after,files,{row['id']:'body.md'},out,source_revision='legacy-1',attachment_bindings={'/old/imported/original.pdf':'original.pdf'})
    with sqlite3.connect(out/'catalog.sqlite3') as db:source_path,storage_path,digest=db.execute('SELECT source_path,storage_path,content_sha256 FROM knowledge_documents').fetchone()
    assert Path(source_path).read_bytes()==original2 and Path(storage_path).read_bytes()==body2 and digest==hashlib.sha256(original2).hexdigest()


def test_pdf_combined_workspace_preserves_original_and_restarts(tmp_path):
    from test_combined_workspace import _wiki
    from knowledge_platform.local.combined_workspace import bootstrap_combined_workspace
    args=pdf_fixture(tmp_path);migrate(args);state=tmp_path/'combined'
    payload=bootstrap_combined_workspace(args[2],_wiki(tmp_path),state)
    assert next(iter(payload['document_bindings'].values())).read_bytes()==args[4]
    with open_persistent_workspace(state) as payload:
        assert next(iter(payload['document_bindings'].values())).read_bytes()==args[4]
    manifest=json.loads((state/'workspace.json').read_text())
    original=state/next(iter(manifest['document_manifest']['original_bindings'].values()))
    assert original.read_bytes()==args[3]


def test_v1_ordinary_candidate_remains_supported(tmp_path):
    from test_document_migration import _run
    _,_,_,candidate,body=_run(tmp_path)
    marker=candidate/'manifest.json';manifest=json.loads(marker.read_text())
    manifest['format']='puddingknowledge-document-migration/v1';manifest['plan'].pop('original_bindings')
    marker.write_text(json.dumps(manifest))
    with open_persistent_workspace(tmp_path/'state',document_migration=candidate) as payload:
        assert next(iter(payload['document_bindings'].values())).read_bytes()==body


def test_old_pdf_candidate_without_original_coverage_rejects(tmp_path):
    args=pdf_fixture(tmp_path);migrate(args);candidate=args[2]
    marker=candidate/'manifest.json';manifest=json.loads(marker.read_text());manifest['format']='puddingknowledge-document-migration/v1'
    originals=manifest['plan'].pop('original_bindings')
    for relative in originals.values():manifest['files'].pop(relative);(candidate/relative).unlink()
    marker.write_text(json.dumps(manifest))
    with pytest.raises(WorkspaceError):
        with open_persistent_workspace(tmp_path/'state',document_migration=candidate):pass
