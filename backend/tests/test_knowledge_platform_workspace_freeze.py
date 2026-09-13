from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from knowledge_platform.local.workspace import WorkspaceError, open_persistent_workspace
from knowledge_platform.local.workspace_freeze import (
    FREEZE_NAME,
    PART_NAME,
    FORMAT,
    freeze_workspace,
)
from test_knowledge_platform_local_workspace import _catalog


def owned_state(tmp_path: Path) -> Path:
    catalog = tmp_path / "source.sqlite3"
    _catalog(catalog)
    wiki = tmp_path / "source-wiki"
    wiki.mkdir()
    (wiki / "guide.md").write_text("# Guide\n", encoding="utf-8")
    state = tmp_path / "state"
    with open_persistent_workspace(state, catalog=catalog, wiki_root=wiki):
        pass
    return state


def test_freeze_requires_existing_owned_workspace_and_is_path_free(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    with pytest.raises(WorkspaceError):
        freeze_workspace(missing, "freeze-1")
    state = owned_state(tmp_path)
    result = freeze_workspace(state, "freeze-1")
    assert result["format"] == FORMAT
    assert result["activation_allowed"] is False
    assert str(tmp_path) not in json.dumps(result)
    marker = state / FREEZE_NAME
    assert marker.exists() and not (state / PART_NAME).exists()
    assert json.loads(marker.read_text()) == {
        **{key: result[key] for key in (
            "activation_allowed", "directory_identity", "format", "operation_id",
            "root_path_sha256", "state", "workspace_manifest_sha256",
        )},
    }


def test_freeze_same_operation_retries_and_other_operation_is_rejected(tmp_path: Path) -> None:
    state = owned_state(tmp_path)
    first = freeze_workspace(state, "freeze-1")
    assert freeze_workspace(state, "freeze-1") == first
    with pytest.raises(WorkspaceError):
        freeze_workspace(state, "freeze-2")


def test_freeze_rejects_while_workspace_is_owned(tmp_path: Path) -> None:
    state = owned_state(tmp_path)
    with open_persistent_workspace(state):
        with pytest.raises(WorkspaceError, match="already owned"):
            freeze_workspace(state, "freeze-1")


@pytest.mark.parametrize("point", ["part", "link"])
def test_freeze_recovers_publication_interruption(tmp_path: Path, point: str) -> None:
    state = owned_state(tmp_path)
    import subprocess,sys,signal
    code=f"from knowledge_platform.local.workspace_freeze import freeze_workspace; import os,signal; freeze_workspace({str(state)!r},'freeze-1',_after_{point}=lambda:os.kill(os.getpid(),signal.SIGKILL))"
    result=subprocess.run([sys.executable,'-c',code],env={**os.environ,'PYTHONPATH':str(Path(__file__).parents[1])})
    assert result.returncode == -signal.SIGKILL
    with pytest.raises(WorkspaceError,match='frozen'):open_persistent_workspace(state)
    assert (state / (PART_NAME if point == "part" else FREEZE_NAME)).exists()
    result = freeze_workspace(state, "freeze-1")
    assert result["activation_allowed"] is False
    assert (state / FREEZE_NAME).exists() and not (state / PART_NAME).exists()


@pytest.mark.parametrize("kind", ["public", "symlink", "hardlink"])
def test_freeze_rejects_tampered_marker(tmp_path: Path, kind: str) -> None:
    state = owned_state(tmp_path)
    marker = state / FREEZE_NAME
    marker.write_text('{"bad":true}\n', encoding="utf-8")
    os.chmod(marker, 0o600)
    if kind == "public":
        os.chmod(marker, 0o644)
    elif kind == "symlink":
        marker.unlink()
        marker.symlink_to(tmp_path / "elsewhere")
    elif kind == "hardlink":
        os.link(marker, tmp_path / "marker-copy")
    with pytest.raises(WorkspaceError):
        freeze_workspace(state, "freeze-1")
    assert marker.is_symlink() if kind == "symlink" else marker.exists()


def test_bad_partial_is_retained_and_ordinary_open_rejects_published_marker(tmp_path: Path) -> None:
    state = owned_state(tmp_path)
    part = state / PART_NAME
    part.write_text("conflicting\n", encoding="utf-8")
    os.chmod(part, 0o600)
    with pytest.raises(WorkspaceError):
        freeze_workspace(state, "freeze-1")
    assert part.exists()
    part.unlink()
    freeze_workspace(state, "freeze-1")
    with pytest.raises(WorkspaceError, match="unexpected|frozen"):
        open_persistent_workspace(state)


def test_schema_wiki_freeze_retry_preserves_owned_manifest_and_catalog(tmp_path):
    from test_wiki_schema_publication import prepare
    owned,state,config,request=prepare(tmp_path)
    before=(state/'catalog.sqlite3').read_bytes(),(state/'workspace.json').read_bytes()
    first=freeze_workspace(state,'wiki-freeze')
    assert freeze_workspace(state,'wiki-freeze')==first
    assert before==((state/'catalog.sqlite3').read_bytes(),(state/'workspace.json').read_bytes())
    with pytest.raises(WorkspaceError,match='frozen'):open_persistent_workspace(state)


def test_manifest_change_cannot_reuse_freeze_receipt(tmp_path):
    state=owned_state(tmp_path);freeze_workspace(state,'original')
    path=state/'workspace.json';path.write_bytes(path.read_bytes()+b' ')
    with pytest.raises(WorkspaceError):freeze_workspace(state,'original')


def test_manifest_changed_during_loader_validation_does_not_publish(tmp_path,monkeypatch):
    from knowledge_platform.local import workspace
    state=owned_state(tmp_path);original=workspace._load_manifest
    def changed(root):
        path=root/'workspace.json';path.write_bytes(path.read_bytes()+b' ')
        return original(root)
    monkeypatch.setattr(workspace,'_load_manifest',changed)
    with pytest.raises(WorkspaceError,match='manifest changed'):freeze_workspace(state,'changed')
    assert not (state/FREEZE_NAME).exists() and not (state/PART_NAME).exists()


def test_manifest_changed_after_part_never_acknowledges_and_stays_frozen(tmp_path):
    state=owned_state(tmp_path)
    def changed():
        path=state/'workspace.json';path.write_bytes(path.read_bytes()+b' ')
    with pytest.raises(WorkspaceError,match='manifest changed'):freeze_workspace(state,'changed',_after_part=changed)
    with pytest.raises(WorkspaceError,match='frozen'):open_persistent_workspace(state)
