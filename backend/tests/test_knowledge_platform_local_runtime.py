from __future__ import annotations

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

from knowledge_platform.local.__main__ import _output_path

ROOT = Path(__file__).resolve().parents[2]


def test_output_refuses_existing_and_symlink(tmp_path):
    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(FileExistsError):
        _output_path(existing)
    link = tmp_path / "link"
    link.symlink_to(existing, target_is_directory=True)
    with pytest.raises(ValueError):
        _output_path(link / "new")


def test_real_local_runtime_and_failure_boundaries(tmp_path):
    python = os.environ.get("KNOWLEDGE_TEST_PYTHON", sys.executable)
    env = {"PATH": os.environ["PATH"]}
    if "KNOWLEDGE_TEST_PYTHON" not in os.environ:
        env["PYTHONPATH"] = str(ROOT / "backend")
    catalog = ROOT / "artifacts/phase0b-local-catalog/knowledge-platform.sqlite3"
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
