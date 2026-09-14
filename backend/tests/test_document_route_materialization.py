import importlib.util,json,sqlite3
from pathlib import Path
import pytest
from test_attachment_rebinding_materialization import attachment_fixture
from knowledge_platform.distribution.document_reverse import prepare_document_reverse


RESOLVER_PATH=Path(__file__).parent/'fixtures/legacy-knowledge-path-resolver.py'
spec=importlib.util.spec_from_file_location('legacy_path_resolver',RESOLVER_PATH)
resolver=importlib.util.module_from_spec(spec);spec.loader.exec_module(resolver)


def route_fixture(tmp_path):
    args,bindings=attachment_fixture(tmp_path)
    for index,path in enumerate(args[:3]):
        table,column=('knowledge_documents','doc_metadata') if index==0 else ('knowledge_assets','metadata_json')
        with sqlite3.connect(path) as db:
            metadata=json.loads(db.execute('SELECT '+column+' FROM '+table).fetchone()[0])
            metadata['multimodal'].update(text_artifact='/knowledge/old/body.md',image_assets_virtual_prefix='/knowledge/old/images')
            db.execute('UPDATE '+table+' SET '+column+'=?',(json.dumps(metadata),))
    return args,bindings


def test_real_legacy_resolver_reads_candidate_routes(tmp_path):
    args,bindings=route_fixture(tmp_path);source=[p.read_bytes() for p in args[:3]]
    receipt=prepare_document_reverse(*args,source_revision='legacy-1',attachment_bindings=bindings)
    root=args[-1]/'bodies'
    with sqlite3.connect(args[-1]/'catalog.sqlite3') as db:
        storage,route,raw=db.execute('SELECT storage_path,virtual_path,doc_metadata FROM knowledge_documents').fetchone()
    metadata=json.loads(raw)
    assert resolver._resolve_virtual_knowledge_path(root,route)==Path(storage)
    assert resolver._resolve_virtual_knowledge_path(root,metadata['multimodal']['text_artifact'])==Path(storage)
    asset=metadata['assets'][0]
    assert resolver._resolve_virtual_knowledge_path(root,asset['virtual_path']).read_bytes()==b'picture fixture'
    assert resolver._resolve_virtual_knowledge_path(root,metadata['multimodal']['image_assets_virtual_prefix'])==Path(metadata['multimodal']['image_assets_dir'])
    assert metadata['nested']['token']=='private-value' and [p.read_bytes() for p in args[:3]]==source
    assert receipt['known_virtual_routes_rebound'] and '/knowledge/' not in json.dumps(receipt['document_routes'])
    assert prepare_document_reverse(*args,source_revision='legacy-1',attachment_bindings=bindings)==dict(receipt,idempotent=True)


def test_virtual_attachment_without_physical_binding_rejects(tmp_path):
    args,bindings=route_fixture(tmp_path)
    with sqlite3.connect(args[2]) as db:
        metadata=json.loads(db.execute('SELECT metadata_json FROM knowledge_assets').fetchone()[0])
        metadata['assets'].append({'virtual_path':'/knowledge/unbound.png'})
        db.execute('UPDATE knowledge_assets SET metadata_json=?',(json.dumps(metadata),))
    with pytest.raises(ValueError):prepare_document_reverse(*args,source_revision='legacy-1',attachment_bindings=bindings)
    assert not (args[-1]/'catalog.sqlite3').exists()


def test_removed_old_attachment_does_not_require_historical_bytes(tmp_path):
    args,bindings=route_fixture(tmp_path)
    with sqlite3.connect(args[2]) as db:
        metadata=json.loads(db.execute('SELECT metadata_json FROM knowledge_assets').fetchone()[0]);metadata.pop('assets');metadata.pop('multimodal')
        db.execute('UPDATE knowledge_assets SET metadata_json=?',(json.dumps(metadata),))
    bindings={key:value for key,value in bindings.items() if key=='/legacy/imported/original.pdf'}
    prepare_document_reverse(*args,source_revision='legacy-1',attachment_bindings=bindings)
    with sqlite3.connect(args[-1]/'catalog.sqlite3') as db:metadata=json.loads(db.execute('SELECT doc_metadata FROM knowledge_documents').fetchone()[0])
    assert 'assets' not in metadata and 'multimodal' not in metadata
