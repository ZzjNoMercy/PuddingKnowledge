from __future__ import annotations

import hashlib
import json
from pathlib import Path
import os
import socket
import subprocess
import sys
import time
from urllib.request import urlopen

import pytest
from sqlalchemy import create_engine, text

from knowledge_platform.catalog.migrations import migrate_to_latest
from knowledge_platform.local.workspace import WorkspaceError, open_persistent_workspace


def _catalog(path: Path) -> None:
    engine = create_engine(f"sqlite:///{path}")
    try:
        with engine.begin() as connection:
            migrate_to_latest(connection)
            connection.execute(text(
                """
                INSERT INTO knowledge_spaces
                    (id, name, description, permissions_json, created_at, updated_at)
                VALUES ('space_kb_default', 'Workspace fixture', 'local', '{}', 'now', 'now')
                """
            ))
            connection.execute(text(
                """
                INSERT INTO knowledge_datasets
                    (id, space_id, name, version, kind, description, asset_ids,
                     semantic_asset_ids, capabilities, freshness, permissions_json,
                     manifest_digest, created_at, updated_at)
                VALUES ('dataset_kb_default', 'space_kb_default', 'Workspace fixture', '1',
                        'wiki', 'local', '[]', '[]', '[]', '{}', '{}', '', 'now', 'now')
                """
            ))
    finally:
        engine.dispose()


def test_state_dir_atomically_owns_sources_and_reuses_them_after_restart(tmp_path: Path) -> None:
    source_catalog = tmp_path / "source.sqlite3"
    _catalog(source_catalog)
    source_wiki = tmp_path / "source-wiki"
    source_wiki.mkdir()
    source_page = source_wiki / "guide.md"
    source_page.write_text("# Original\n", encoding="utf-8")
    state_dir = tmp_path / "state"
    source_digest = hashlib.sha256(source_catalog.read_bytes()).hexdigest()

    with open_persistent_workspace(state_dir, catalog=source_catalog, wiki_root=source_wiki) as first:
        owned_catalog = first["catalog"]
        owned_page = first["file_bindings"][next(iter(first["file_bindings"]))]
        assert owned_catalog == state_dir / "catalog.sqlite3"
        assert owned_page == state_dir / "wiki" / "guide.md"
        assert owned_page.read_text(encoding="utf-8") == "# Original\n"
        assert hashlib.sha256(source_catalog.read_bytes()).hexdigest() == source_digest

    # The external inputs may disappear or change after the first start.
    source_catalog.unlink()
    source_page.write_text("# Changed outside owned state\n", encoding="utf-8")
    with open_persistent_workspace(state_dir) as second:
        assert second["catalog"] == owned_catalog
        assert second["wiki_root"] == state_dir / "wiki"
        assert second["file_bindings"]
        assert next(iter(second["file_bindings"].values())).read_text(encoding="utf-8") == "# Original\n"
        manifest = json.loads((state_dir / "workspace.json").read_text(encoding="utf-8"))
        assert manifest["catalog"] == "catalog.sqlite3"
        assert manifest["wiki_root"] == "wiki"
        assert not (state_dir / ".initializing").exists()


def test_local_cli_restarts_from_owned_state_without_external_sources(tmp_path: Path) -> None:
    source_catalog = tmp_path / "source.sqlite3"
    _catalog(source_catalog)
    source_wiki = tmp_path / "source-wiki"
    source_wiki.mkdir()
    (source_wiki / "guide.md").write_text(
        "---\ntitle: Persistent guide\n---\n# Guide\n", encoding="utf-8"
    )
    state_dir = tmp_path / "state"
    python = os.environ.get("KNOWLEDGE_TEST_PYTHON", sys.executable)
    environment = {
        "PATH": os.environ["PATH"],
        "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "backend"),
    }

    def free_port() -> int:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            return int(listener.getsockname()[1])

    def launch(port: int, ready: Path, *, include_sources: bool) -> subprocess.Popen[bytes]:
        args = [
            python, "-m", "knowledge_platform.local", "--state-dir", str(state_dir),
            "--temp-dir", str(tmp_path / f"workspace-{port}"), "--ready-file", str(ready),
            "--port", str(port),
        ]
        if include_sources:
            args.extend(("--catalog", str(source_catalog), "--wiki-root", str(source_wiki)))
        return subprocess.Popen(
            args, cwd=tmp_path, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )

    def wait_for_assets(child: subprocess.Popen[bytes], port: int, ready: Path) -> dict:
        for _ in range(100):
            if child.poll() is not None:
                raise AssertionError(child.communicate()[1].decode())
            try:
                with urlopen(
                    f"http://127.0.0.1:{port}/v1/assets?space_id=space_kb_default", timeout=.2
                ) as response:
                    assert ready.exists()
                    return json.load(response)
            except OSError:
                time.sleep(.05)
        raise AssertionError("local state-dir runtime did not become ready")

    first_port = free_port()
    first_ready = tmp_path / "first-ready.json"
    first = launch(first_port, first_ready, include_sources=True)
    try:
        first_payload = wait_for_assets(first, first_port, first_ready)
        assert any(asset["title"] == "Persistent guide" for asset in first_payload["data"]["assets"])
    finally:
        first.terminate()
        first.communicate(timeout=10)
    assert not first_ready.exists()

    source_catalog.unlink()
    (source_wiki / "guide.md").unlink()
    source_wiki.rmdir()
    second_port = free_port()
    second_ready = tmp_path / "second-ready.json"
    second = launch(second_port, second_ready, include_sources=False)
    try:
        second_payload = wait_for_assets(second, second_port, second_ready)
        assert any(asset["title"] == "Persistent guide" for asset in second_payload["data"]["assets"])
    finally:
        second.terminate()
        second.communicate(timeout=10)


def test_state_dir_lock_rejects_second_process_and_partial_initialization(tmp_path: Path) -> None:
    source_catalog = tmp_path / "source.sqlite3"
    _catalog(source_catalog)
    source_wiki = tmp_path / "wiki"
    source_wiki.mkdir()
    (source_wiki / "page.md").write_text("# Page\n", encoding="utf-8")
    state_dir = tmp_path / "state"

    with open_persistent_workspace(state_dir, catalog=source_catalog, wiki_root=source_wiki):
        with pytest.raises(WorkspaceError, match="already owned"):
            open_persistent_workspace(state_dir)

    partial = tmp_path / "partial"
    partial.mkdir()
    (partial / ".initializing").write_text("in progress", encoding="utf-8")
    with pytest.raises(WorkspaceError, match="incomplete"):
        open_persistent_workspace(partial)


@pytest.mark.parametrize("kind", ["symlink", "fifo"])
def test_state_dir_rejects_non_directory_boundary(tmp_path: Path, kind: str) -> None:
    target = tmp_path / "state"
    if kind == "symlink":
        real = tmp_path / "real"
        real.mkdir()
        target.symlink_to(real, target_is_directory=True)
    else:
        os.mkfifo(target)
    with pytest.raises(WorkspaceError, match="symlink|real directory"):
        open_persistent_workspace(target)


def test_unrelated_directory_rejected_without_permissions_or_lock_mutation(tmp_path):
    from knowledge_platform.local.workspace import open_persistent_workspace, WorkspaceError
    import stat
    unrelated = tmp_path / 'unrelated'
    unrelated.mkdir(mode=0o755)
    (unrelated / 'user.txt').write_text('keep')
    mode = stat.S_IMODE(unrelated.stat().st_mode)
    with pytest.raises(WorkspaceError):
        open_persistent_workspace(unrelated)
    assert stat.S_IMODE(unrelated.stat().st_mode) == mode
    assert sorted(p.name for p in unrelated.iterdir()) == ['user.txt']
