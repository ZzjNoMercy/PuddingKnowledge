import hashlib
import json
from pathlib import Path
import sqlite3

import pytest
from knowledge_platform.distribution.document_reverse import prepare_document_reverse
from test_document_dependency_materialization import relative_fixture


def attachment_fixture(tmp_path):
    args=relative_fixture(tmp_path);root=args[3]
    (root/'original').mkdir();(root/'original/source.pdf').write_bytes(b'%PDF-original-file-fixture')
    (root/'images/empty').mkdir()
    image=(root/'images/pic#x.png').read_bytes();original=(root/'original/source.pdf').read_bytes()
    attachments={'assets':[{'path':'/legacy/attachments/pic#x.png','sha256':hashlib.sha256(image).hexdigest(),
                           'size_bytes':len(image),'virtual_path':'/knowledge/assets/old/pic#x.png'}],
                 'original_path':'/legacy/imported/original.pdf','original_sha256':hashlib.sha256(original).hexdigest(),
                 'multimodal':{'image_assets_dir':'/legacy/attachments','image_asset_count':1}}
    for index,path in enumerate(args[:3]):
        table,column=('knowledge_documents','doc_metadata') if index==0 else ('knowledge_assets','metadata_json')
        with sqlite3.connect(path) as db:
            metadata=json.loads(db.execute('SELECT '+column+' FROM '+table).fetchone()[0]);metadata.update(attachments)
            if index:
                from knowledge_platform.catalog.rehearsal_runner import _redact
                metadata=_redact(metadata)
            db.execute('UPDATE '+table+' SET '+column+'=?',(json.dumps(metadata),))
    bindings={'/legacy/attachments/pic#x.png':'images/pic#x.png',
              '/legacy/imported/original.pdf':'original/source.pdf',
              '/legacy/attachments':'images'}
    return args,bindings


def run(args,bindings,**kwargs):
    return prepare_document_reverse(*args,source_revision='legacy-1',attachment_bindings=bindings,**kwargs)


def test_known_file_and_directory_metadata_paths_are_bound_to_candidate(tmp_path):
    args,bindings=attachment_fixture(tmp_path);original=[p.read_bytes() for p in args[:3]];receipt=run(args,bindings)
    with sqlite3.connect(args[-1]/'catalog.sqlite3') as db:
        metadata=json.loads(db.execute('SELECT doc_metadata FROM knowledge_documents').fetchone()[0])
    asset=metadata['assets'][0];asset_path=Path(asset['path']);pdf_path=Path(metadata['original_path'])
    assert asset_path.is_relative_to(args[-1]/'bodies') and asset_path.read_bytes()==b'picture fixture'
    assert pdf_path.is_relative_to(args[-1]/'bodies') and pdf_path.read_bytes()==b'%PDF-original-file-fixture'
    directory=Path(metadata['multimodal']['image_assets_dir'])
    assert directory==asset_path.parent and (directory/'empty').is_dir()
    assert metadata['nested']['token']=='private-value'
    assert metadata['assets'][0]['virtual_path']=='/knowledge/assets/old/pic#x.png'
    assert receipt['known_attachment_filesystem_paths_rebound'] is True
    assert receipt['metadata_attachment_paths_rebound'] is False  # virtual routing is still installation-owned
    assert '/legacy/' not in json.dumps(receipt)
    assert [p.read_bytes() for p in args[:3]]==original
    assert run(args,bindings)==dict(receipt,idempotent=True)


@pytest.mark.parametrize('mode',['missing','extra','escape','symlink'])
def test_invalid_explicit_binding_is_rejected(tmp_path,mode):
    args,bindings=attachment_fixture(tmp_path)
    if mode=='missing':bindings.pop('/legacy/attachments')
    elif mode=='extra':bindings['/unused/path']='images/pic#x.png'
    elif mode=='escape':bindings['/legacy/attachments']='../outside'
    else:
        (args[3]/'linked').symlink_to(args[3]/'images',target_is_directory=True)
        bindings['/legacy/attachments']='linked'
    with pytest.raises((ValueError,OSError)):run(args,bindings)
    assert not args[-1].exists()


def test_metadata_hash_mismatch_is_not_overwritten(tmp_path):
    args,bindings=attachment_fixture(tmp_path)
    with sqlite3.connect(args[2]) as db:
        metadata=json.loads(db.execute('SELECT metadata_json FROM knowledge_assets').fetchone()[0]);metadata['assets'][0]['sha256']='0'*64
        db.execute('UPDATE knowledge_assets SET metadata_json=?',(json.dumps(metadata),))
    with pytest.raises(ValueError):run(args,bindings)
    assert not (args[-1]/'catalog.sqlite3').exists()


def test_directory_added_file_after_copy_cannot_complete(tmp_path):
    args,bindings=attachment_fixture(tmp_path)
    def add_file(name):
        if name=='catalog.sqlite3':(args[3]/'images/late.png').write_bytes(b'late')
    with pytest.raises(ValueError,match='directory changed'):run(args,bindings,_after_copy=add_file)
    assert json.loads((args[-1]/'manifest.json').read_text())['state']=='copying'


def test_missing_completed_original_attachment_is_not_repaired(tmp_path):
    args,bindings=attachment_fixture(tmp_path);run(args,bindings)
    target=args[-1]/'bodies/original/source.pdf';target.unlink();manifest=(args[-1]/'manifest.json').read_bytes()
    with pytest.raises(ValueError):run(args,bindings)
    assert not target.exists() and (args[-1]/'manifest.json').read_bytes()==manifest


def test_directory_and_file_mapping_disagreement_rejects(tmp_path):
    args,bindings=attachment_fixture(tmp_path);(args[3]/'other').mkdir();(args[3]/'other/pic#x.png').write_bytes(b'picture fixture')
    bindings['/legacy/attachments/pic#x.png']='other/pic#x.png'
    with pytest.raises(ValueError,match='bindings disagree'):run(args,bindings)


def test_changed_attachment_content_uses_current_claim_and_preserves_source(tmp_path):
    args,bindings=attachment_fixture(tmp_path);source=args[0].read_bytes();body=b'new picture content'
    (args[3]/'images/pic#x.png').write_bytes(body)
    with sqlite3.connect(args[2]) as db:
        metadata=json.loads(db.execute('SELECT metadata_json FROM knowledge_assets').fetchone()[0])
        metadata['assets'][0]['sha256']=hashlib.sha256(body).hexdigest();metadata['assets'][0]['size_bytes']=len(body)
        db.execute('UPDATE knowledge_assets SET metadata_json=?',(json.dumps(metadata),))
    run(args,bindings)
    with sqlite3.connect(args[-1]/'catalog.sqlite3') as db:metadata=json.loads(db.execute('SELECT doc_metadata FROM knowledge_documents').fetchone()[0])
    assert Path(metadata['assets'][0]['path']).read_bytes()==body
    assert metadata['assets'][0]['sha256']==hashlib.sha256(body).hexdigest()
    assert args[0].read_bytes()==source


def test_attachment_fact_and_graph_inspection_cannot_capture_different_bytes(tmp_path,monkeypatch):
    from knowledge_platform.distribution import document_dependencies
    args,bindings=attachment_fixture(tmp_path);original=document_dependencies.collect_document_dependencies
    def mutate(root,primary):
        (root/'original/source.pdf').write_bytes(b'changed between inspections')
        return original(root,primary)
    monkeypatch.setattr(document_dependencies,'collect_document_dependencies',mutate)
    with pytest.raises(ValueError,match='changed during dependency discovery'):run(args,bindings)
    assert not args[-1].exists()
