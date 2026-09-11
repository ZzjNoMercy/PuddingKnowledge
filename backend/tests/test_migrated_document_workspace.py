from __future__ import annotations

import json
from pathlib import Path

import pytest

from knowledge_platform.local.migrated_documents import (
    MigratedDocumentWorkspaceError,
    bootstrap_migrated_documents,
)
from knowledge_platform.local.workspace import open_persistent_workspace

from test_document_migration import _run


def test_candidate_bootstraps_and_restarts_without_candidate(tmp_path: Path) -> None:
    _, _, _, candidate, body = _run(tmp_path)
    state = tmp_path / "state"
    with open_persistent_workspace(state, document_migration=candidate) as payload:
        assert payload["pages"] == 0
        assert payload["document_bindings"] == payload["file_bindings"]
        assert next(iter(payload["file_bindings"].values())).read_bytes() == body
    candidate.rename(tmp_path / "candidate-moved")
    with open_persistent_workspace(state) as payload:
        assert next(iter(payload["document_bindings"].values())).read_bytes() == body


def test_candidate_manifest_and_blob_tampering_is_rejected(tmp_path: Path) -> None:
    _, _, _, candidate, _ = _run(tmp_path)
    manifest = json.loads((candidate / "manifest.json").read_text())
    relative = next(iter(manifest["asset_bindings"].values()))
    (candidate / relative).write_bytes(b"tampered")
    with pytest.raises(MigratedDocumentWorkspaceError):
        bootstrap_migrated_documents(candidate, tmp_path / "state")
    assert not (tmp_path / "state" / "catalog.sqlite3").exists()


def test_state_tampering_is_rejected_on_restart(tmp_path: Path) -> None:
    _, _, _, candidate, _ = _run(tmp_path)
    state = tmp_path / "state"
    with open_persistent_workspace(state, document_migration=candidate):
        pass
    manifest = json.loads((state / "workspace.json").read_text())
    manifest["facts"]["assets"][next(iter(manifest["facts"]["assets"]))]["content_digest"] = "sha256:" + "0" * 64
    (state / "workspace.json").write_text(json.dumps(manifest))
    with pytest.raises(Exception):
        open_persistent_workspace(state)


@pytest.mark.parametrize('change', ['format','active','hardlink','symlink','escape','cross_space'])
def test_candidate_boundaries_and_failed_validation_release_lock(tmp_path, change):
    import fcntl
    import hashlib
    import os
    import sqlite3
    from knowledge_platform.local.workspace import WorkspaceError
    _, _, _, candidate, _ = _run(tmp_path)
    manifest_path = candidate/'manifest.json'
    manifest = json.loads(manifest_path.read_text())
    relative = next(iter(manifest['asset_bindings'].values()))
    if change == 'format': manifest['format'] = 'unknown/v1'
    if change == 'active': manifest['activation_allowed'] = 0
    if change == 'hardlink': os.link(candidate/relative, tmp_path/'alias')
    if change == 'symlink':
        (candidate/relative).rename(tmp_path/'external'); (candidate/relative).symlink_to(tmp_path/'external')
    if change == 'escape': manifest['asset_bindings'][next(iter(manifest['asset_bindings']))] = '../external'
    if change == 'cross_space':
        with sqlite3.connect(candidate/'catalog.sqlite3') as db:
            db.execute("UPDATE knowledge_datasets SET space_id='another-space'")
        manifest['files']['catalog.sqlite3'] = 'sha256:'+hashlib.sha256((candidate/'catalog.sqlite3').read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(WorkspaceError): open_persistent_workspace(tmp_path/'state', document_migration=candidate)
    # Failure cannot strand the cooperating publisher's lock in this process.
    with (candidate/'.migration.lock').open('r+b') as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_restart_checks_blob_and_trace_symlinks(tmp_path):
    from knowledge_platform.local.workspace import WorkspaceError
    _, _, _, candidate, _ = _run(tmp_path)
    state = tmp_path/'state'
    with open_persistent_workspace(state, document_migration=candidate) as data:
        body = next(iter(data['file_bindings'].values()))
    external = tmp_path/'external'; external.write_text('private'); external.chmod(0o600)
    trace = state/'retrieval-traces.sqlite3'; trace.symlink_to(external)
    with pytest.raises(WorkspaceError): open_persistent_workspace(state)
    trace.unlink()
    body.write_text('tampered')
    with pytest.raises(WorkspaceError): open_persistent_workspace(state)


def test_candidate_busy_and_existing_workspace_are_rejected(tmp_path):
    import fcntl
    from knowledge_platform.local.workspace import WorkspaceError
    _, _, _, candidate, _ = _run(tmp_path)
    with (candidate/'.migration.lock').open('r+b') as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(WorkspaceError): open_persistent_workspace(tmp_path/'busy', document_migration=candidate)
    with open_persistent_workspace(tmp_path/'state', document_migration=candidate): pass
    with pytest.raises(WorkspaceError): open_persistent_workspace(tmp_path/'state', document_migration=candidate)


def test_later_unrelated_catalog_rows_do_not_invalidate_migration(tmp_path):
    import sqlite3
    _, _, _, candidate, _ = _run(tmp_path)
    state = tmp_path/'state'
    with open_persistent_workspace(state, document_migration=candidate): pass
    with sqlite3.connect(state/'catalog.sqlite3') as db:
        db.execute('CREATE TABLE later_feature(id TEXT PRIMARY KEY, value TEXT)')
        db.execute("INSERT INTO later_feature VALUES ('new','value')")
    with open_persistent_workspace(state) as data:
        assert data['document_bindings']


def test_interrupted_initialization_cannot_be_adopted(tmp_path, monkeypatch):
    from knowledge_platform.local import migrated_documents as module
    from knowledge_platform.local.workspace import WorkspaceError
    _, _, _, candidate, _ = _run(tmp_path)
    original = module._write
    def fail(path, data):
        if path.name == 'workspace.json': raise OSError('simulated failure after copy')
        return original(path, data)
    monkeypatch.setattr(module, '_write', fail)
    with pytest.raises(WorkspaceError): open_persistent_workspace(tmp_path/'state', document_migration=candidate)
    assert (tmp_path/'state/.initializing').exists()
    with pytest.raises(WorkspaceError): open_persistent_workspace(tmp_path/'state')


@pytest.mark.parametrize('provider', ['unknown', 'knowledge_local_vector'])
def test_restart_validates_routing_but_allows_managed_vector_binding(tmp_path, provider):
    import sqlite3
    from knowledge_platform.local.workspace import WorkspaceError
    _, _, _, candidate, _ = _run(tmp_path)
    state = tmp_path/'state'
    with open_persistent_workspace(state, document_migration=candidate): pass
    with sqlite3.connect(state/'catalog.sqlite3') as db:
        db.execute('UPDATE knowledge_collection_bindings SET binding_json=?', (json.dumps({'provider_id':provider}),))
    if provider == 'unknown':
        with pytest.raises(WorkspaceError): open_persistent_workspace(state)
    else:
        with open_persistent_workspace(state) as data: assert data['document_bindings']
