from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import Request, urlopen

import pytest
import uvicorn
from sqlalchemy import create_engine, text

from knowledge_platform.catalog.migrations import migrate_to_latest
from knowledge_platform.local.__main__ import LocalServer, _output_path

ROOT = Path(__file__).resolve().parents[2]


def _build_minimal_catalog(path: Path) -> None:
    """Create the smallest real Platform Catalog accepted by the local CLI.

    The local runtime test must own its input database.  Reusing a generated
    Phase 0B artifact would make an extracted Knowledge repository depend on
    the source checkout's ignored files and could silently test the wrong
    schema.  Use the target migration runner so this fixture exercises the
    same Platform-owned schema contract as the subprocess under test.
    """

    engine = create_engine(f"sqlite:///{path}")
    try:
        with engine.begin() as connection:
            migrate_to_latest(connection)
            connection.execute(
                text(
                    """
                    INSERT INTO knowledge_spaces
                        (id, name, description, permissions_json, created_at, updated_at)
                    VALUES (:id, :name, :description, :permissions, :created_at, :updated_at)
                    """
                ),
                {
                    "id": "space_kb_default",
                    "name": "Local Knowledge",
                    "description": "Target-owned runtime fixture",
                    "permissions": "{}",
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "updated_at": "2026-01-01T00:00:00+00:00",
                },
            )
            connection.execute(
                text(
                    """
                    INSERT INTO knowledge_datasets
                        (id, space_id, name, version, kind, description, asset_ids,
                         semantic_asset_ids, capabilities, freshness, permissions_json,
                         manifest_digest, created_at, updated_at)
                    VALUES (:id, :space_id, :name, :version, :kind, :description, :asset_ids,
                            :semantic_asset_ids, :capabilities, :freshness, :permissions,
                            :manifest_digest, :created_at, :updated_at)
                    """
                ),
                {
                    "id": "dataset_kb_default",
                    "space_id": "space_kb_default",
                    "name": "Local Knowledge",
                    "version": "1",
                    "kind": "wiki",
                    "description": "Target-owned runtime fixture",
                    "asset_ids": "[]",
                    "semantic_asset_ids": "[]",
                    "capabilities": "[]",
                    "freshness": "{}",
                    "permissions": "{}",
                    "manifest_digest": "",
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "updated_at": "2026-01-01T00:00:00+00:00",
                },
            )
    finally:
        engine.dispose()


def test_output_refuses_existing_and_symlink(tmp_path):
    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(FileExistsError):
        _output_path(existing)
    link = tmp_path / "link"
    link.symlink_to(existing, target_is_directory=True)
    with pytest.raises(ValueError):
        _output_path(link / "new")


def test_ready_receipt_is_published_after_listener_startup(tmp_path, monkeypatch):
    ready = tmp_path / "ready.json"
    observed = []

    async def start_listener(server, sockets=None):
        observed.append(ready.exists())
        server.started = True

    monkeypatch.setattr(uvicorn.Server, "startup", start_listener)
    server = LocalServer(
        uvicorn.Config("unused:app"),
        ready_path=ready,
        ready_payload={"status": "ready", "activation_allowed": False},
    )
    asyncio.run(server.startup())

    assert observed == [False]
    assert json.loads(ready.read_text()) == {
        "status": "ready",
        "activation_allowed": False,
    }


def test_real_local_runtime_and_failure_boundaries(tmp_path):
    python = os.environ.get("KNOWLEDGE_TEST_PYTHON", sys.executable)
    env = {"PATH": os.environ["PATH"]}
    if "KNOWLEDGE_TEST_PYTHON" not in os.environ:
        env["PYTHONPATH"] = str(ROOT / "backend")
    catalog = tmp_path / "catalog.sqlite3"
    _build_minimal_catalog(catalog)
    before = hashlib.sha256(catalog.read_bytes()).hexdigest()
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "page.md").write_text("---\ntitle: Runtime validation\n---\n# Runtime validation\n\nindependent boundary evidence\n")
    ready = tmp_path / "ready.json"
    workspace = tmp_path / "workspace"
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
    args = [python, "-m", "knowledge_platform.local", "--catalog", str(catalog),
            "--wiki-root", str(wiki), "--temp-dir", str(workspace), "--port", str(port),
            "--ready-file", str(ready)]
    with subprocess.Popen(args, cwd=tmp_path, env=env, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE) as child:
        try:
            for _ in range(100):
                if child.poll() is not None:
                    pytest.fail(child.communicate()[1].decode())
                try:
                    with urlopen(f"http://127.0.0.1:{port}/v1/spaces", timeout=.2) as response:
                        assert json.load(response)["status"] == "ok"
                    break
                except OSError:
                    time.sleep(.1)
            else:
                pytest.fail("HTTP startup timed out")
            assert json.loads(ready.read_text())["activation_allowed"] is False
            with urlopen(f"http://127.0.0.1:{port}/v1/assets?space_id=space_kb_default") as response:
                payload = json.load(response)
                assert payload["status"] == "ok"
            asset = next(a for a in payload["data"]["assets"] if a["title"] == "Runtime validation")
            request = Request(f"http://127.0.0.1:{port}/v1/assets/{asset['id']}:read",
                              data=json.dumps({"start": 0, "end": 4096}).encode(),
                              headers={"Content-Type": "application/json"})
            with urlopen(request) as response:
                read = json.load(response)
                assert read["status"] == "ok"
                assert b"independent boundary evidence" in base64.b64decode(read["data"]["content_base64"])
            request = Request(f"http://127.0.0.1:{port}/v1/search",
                              data=json.dumps({"query": "Runtime validation", "space_id": "space_kb_default"}).encode(),
                              headers={"Content-Type": "application/json"})
            with urlopen(request) as response:
                search = json.load(response)
                assert search["status"] == "ok"
                assert asset["id"] in json.dumps(search)
            # An occupied port must fail before creating staging or readiness.
            rejected = args.copy()
            rejected[rejected.index("--temp-dir") + 1] = str(tmp_path / "collision")
            rejected[rejected.index("--ready-file") + 1] = str(tmp_path / "collision.json")
            result = subprocess.run(rejected, cwd=tmp_path, env=env, capture_output=True, timeout=10)
            assert result.returncode != 0
            assert not (tmp_path / "collision").exists()
            assert not (tmp_path / "collision.json").exists()
        finally:
            child.terminate()
            child.communicate(timeout=10)
    assert not ready.exists()
    assert hashlib.sha256(catalog.read_bytes()).hexdigest() == before
    # A used workspace is never silently recycled.
    result = subprocess.run(args, cwd=tmp_path, env=env, capture_output=True, timeout=10)
    assert result.returncode != 0
