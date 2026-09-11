from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from knowledge_platform.distribution.wiki_archive import prepare_wiki_archive
from knowledge_platform.local.combined_workspace import (
    CombinedWorkspaceError,
    bootstrap_combined_workspace,
    load_combined_workspace,
)
from knowledge_platform.local.workspace import open_persistent_workspace

from test_document_migration import _run


def _wiki(tmp_path: Path, installation_id: str = "install-wiki") -> Path:
    source = tmp_path / "brain"
    (source / "wiki").mkdir(parents=True)
    (source / "wiki" / "page.md").write_text("wiki body", encoding="utf-8")
    raw = b"history"
    (source / "raw").mkdir()
    (source / "raw" / "page.txt").write_bytes(raw)
    (source / "raw" / "manifest.jsonl").write_text(json.dumps({
        "snapshot_path": "page.txt", "sha256": hashlib.sha256(raw).hexdigest(), "size_bytes": len(raw)
    }) + "\n")
    out = tmp_path / "archive"
    prepare_wiki_archive(source, out, installation_id=installation_id, source_revision="r1")
    return out


def _make(tmp_path: Path):
    _result, _catalog, _files, document, document_body = _run(tmp_path)
    return document, _wiki(tmp_path), document_body


def test_bootstrap_has_one_catalog_and_both_bindings(tmp_path: Path):
    document, wiki, document_body = _make(tmp_path)
    state = tmp_path / "state"
    result = bootstrap_combined_workspace(document, wiki, state)
    manifest = json.loads((state / "workspace.json").read_text())
    assert manifest["version"] == 4 and manifest["kind"] == "migrated_knowledge"
    assert set(manifest) == {"version", "owner", "kind", "catalog", "blob_root", "evidence_root", "document_manifest", "wiki_manifest", "activation_allowed"}
    assert len(result["document_bindings"]) == 1 and len(result["wiki_bindings"]) == 1
    assert result["document_bindings"]
    assert next(iter(result["document_bindings"].values())).read_bytes() == document_body
    assert next(iter(result["wiki_bindings"].values())).read_text() == "wiki body"
    with sqlite3.connect(state / "catalog.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM knowledge_spaces").fetchone()[0] == 2


def test_source_can_move_and_workspace_restarts(tmp_path: Path):
    document, wiki, _ = _make(tmp_path)
    state = tmp_path / "state"
    bootstrap_combined_workspace(document, wiki, state)
    document.rename(tmp_path / "document-moved")
    wiki.rename(tmp_path / "wiki-moved")
    with open_persistent_workspace(state) as result:
        assert result["document_bindings"] and result["wiki_bindings"]


def test_identity_collision_is_rejected_without_publishing(tmp_path: Path):
    document, wiki, _ = _make(tmp_path)
    # The Wiki space identity is deterministic; inject the same id into the
    # document Catalog so the merge must fail closed rather than overwrite.
    from knowledge_platform.local.migrated_documents import bootstrap_migrated_documents
    from knowledge_platform.local.migrated_wiki import bootstrap_migrated_wiki
    ds, ws = tmp_path / "ds", tmp_path / "ws"
    bootstrap_migrated_documents(document, ds)
    bootstrap_migrated_wiki(wiki, ws)
    with sqlite3.connect(ws / "catalog.sqlite3") as source:
        space_id = source.execute("SELECT id FROM knowledge_spaces").fetchone()[0]
    with sqlite3.connect(ds / "catalog.sqlite3") as target:
        target.execute("INSERT INTO knowledge_spaces SELECT ?, 'Collision', description, permissions_json, created_at, updated_at FROM knowledge_spaces LIMIT 1", (space_id,))
        target.commit()
    from knowledge_platform.local.combined_workspace import _merge_catalog
    with pytest.raises(CombinedWorkspaceError, match="identity collision"):
        _merge_catalog(ds / "catalog.sqlite3", ws / "catalog.sqlite3", tmp_path / "merged.sqlite3")
    with sqlite3.connect(ds / "catalog.sqlite3") as db:
        assert db.execute("SELECT name FROM knowledge_spaces WHERE id=?", (space_id,)).fetchone()[0] == "Collision"


def test_partial_marker_fails_closed(tmp_path: Path):
    state = tmp_path / "state"
    state.mkdir()
    (state / ".initializing").write_bytes(b"partial")
    with pytest.raises(CombinedWorkspaceError):
        load_combined_workspace(state, {})


def test_changed_source_before_final_validation_keeps_marker(tmp_path,monkeypatch):
    from knowledge_platform.local import combined_workspace as module
    document,wiki,_=_make(tmp_path);state=tmp_path/'state'
    original=module.load_combined_workspace
    def change(*args,**kwargs):
        result=original(*args,**kwargs)
        path=document/'manifest.json';path.write_bytes(path.read_bytes()+b' ')
        return result
    monkeypatch.setattr(module,'load_combined_workspace',change)
    with pytest.raises(CombinedWorkspaceError,match='inputs changed'):
        module.bootstrap_combined_workspace(document,wiki,state)
    assert (state/'.initializing').exists()
    with pytest.raises(Exception):open_persistent_workspace(state)


def test_actual_sigkill_during_publication_is_not_complete(tmp_path):
    import os,signal,subprocess,sys
    document,wiki,_=_make(tmp_path);state=tmp_path/'state'
    code='''
import os,signal,sys
from pathlib import Path
from knowledge_platform.local.workspace import open_persistent_workspace
replace=os.replace
state=Path(sys.argv[3])
def kill(src,dst):
 result=replace(src,dst)
 if Path(dst)==state/'workspace.json':os.kill(os.getpid(),signal.SIGKILL)
 return result
os.replace=kill
open_persistent_workspace(state,document_migration=Path(sys.argv[1]),wiki_archive=Path(sys.argv[2]))
'''
    result=subprocess.run([sys.executable,'-c',code,str(document),str(wiki),str(state)],capture_output=True)
    assert result.returncode == -signal.SIGKILL,result.stderr
    assert (state/'.initializing').exists()
    with pytest.raises(Exception):open_persistent_workspace(state)


@pytest.mark.parametrize('change',['document','wiki','nested_manifest','unknown'])
def test_joint_workspace_tampering_rejected(tmp_path,change):
    document,wiki,_=_make(tmp_path);state=tmp_path/'state'
    result=bootstrap_combined_workspace(document,wiki,state)
    if change=='document':next(iter(result['document_bindings'].values())).write_bytes(b'bad')
    if change=='wiki':next(iter(result['wiki_bindings'].values())).write_bytes(b'bad')
    if change=='unknown':(state/'unowned').write_bytes(b'keep')
    if change=='nested_manifest':
        path=state/'workspace.json';manifest=json.loads(path.read_text());manifest['wiki_manifest']=manifest['document_manifest'];path.write_text(json.dumps(manifest))
    with pytest.raises(Exception):open_persistent_workspace(state)


@pytest.mark.parametrize('change',['schema_version','unknown_table'])
def test_merge_rejects_schema_drift(tmp_path,change):
    from knowledge_platform.local.combined_workspace import _merge_catalog
    from knowledge_platform.local.migrated_documents import bootstrap_migrated_documents
    from knowledge_platform.local.migrated_wiki import bootstrap_migrated_wiki
    document,wiki,_=_make(tmp_path)
    ds,ws=tmp_path/'ds',tmp_path/'ws'
    bootstrap_migrated_documents(document,ds);bootstrap_migrated_wiki(wiki,ws)
    with sqlite3.connect(ws/'catalog.sqlite3') as db:
        if change=='schema_version':db.execute('DELETE FROM knowledge_catalog_schema_versions WHERE version=1')
        else:db.execute('CREATE TABLE unknown_data(value TEXT)')
    with pytest.raises(CombinedWorkspaceError,match='schema'):
        _merge_catalog(ds/'catalog.sqlite3',ws/'catalog.sqlite3',tmp_path/'merged.sqlite3')
