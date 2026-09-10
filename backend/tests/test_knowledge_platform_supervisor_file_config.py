from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys

from sqlalchemy import create_engine, text
from knowledge_platform.catalog.migrations import migrate_to_latest

ROOT = Path(__file__).resolve().parents[2]
PYTHON = os.environ.get("KNOWLEDGE_TEST_PYTHON", sys.executable)


def _port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_real_supervisor_passes_file_config_to_runtime(tmp_path: Path):
    catalog = tmp_path / "catalog.sqlite3"
    engine = create_engine(f"sqlite:///{catalog}")
    with engine.begin() as conn:
        migrate_to_latest(conn)
        conn.execute(text("INSERT INTO knowledge_spaces (id,name,description,permissions_json,created_at,updated_at) VALUES ('space_kb_default','Default','local','{}','now','now')"))
        conn.execute(text("INSERT INTO knowledge_datasets (id,space_id,name,version,kind,description,asset_ids,semantic_asset_ids,capabilities,freshness,permissions_json,manifest_digest,created_at,updated_at) VALUES ('dataset_kb_default','space_kb_default','Default','1','wiki','local','[]','[]','[]','{}','{}','','now','now')"))
    engine.dispose()
    source = tmp_path / "note.md"
    source.write_text("# file\n", encoding="utf-8")
    file_config = tmp_path / "files.json"
    file_config.write_text(json.dumps({"version": 1, "bindings": [{"id": "b", "path": str(source), "space_id": "space_kb_default"}], "parsers": [{"id": "native"}], "collection_id": "uploaded_files"}), encoding="utf-8")
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "published.md").write_text("# published\n", encoding="utf-8")
    home = tmp_path / "home"
    env = {"PATH": os.environ["PATH"], "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(ROOT / "backend")}
    args = [PYTHON, "-m", "knowledge_platform.local.supervisor", "start", "--home", str(home), "--catalog", str(catalog), "--wiki-root", str(wiki), "--state-dir", str(tmp_path / "state"), "--file-config", str(file_config), "--port", str(_port())]
    stop_args = [PYTHON, "-m", "knowledge_platform.local.supervisor", "stop", "--home", str(home)]
    started = subprocess.run(args, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=45)
    assert started.returncode == 0, started.stderr
    try:
        observation = json.loads(started.stdout)
        assert observation["state"] == "running" and observation["health"] is True
        stopped = subprocess.run(stop_args, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=45)
        assert stopped.returncode == 0, stopped.stderr
        restarted = subprocess.run(args, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=45)
        assert restarted.returncode == 0, restarted.stderr
        assert json.loads(restarted.stdout)["state"] == "running"
    finally:
        subprocess.run(stop_args, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=45)
