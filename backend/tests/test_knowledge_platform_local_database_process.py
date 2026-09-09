"""Real independent-runtime HTTP replay against an owned temporary PostgreSQL."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from test_knowledge_platform_local_database import DIGEST, database_config
from test_knowledge_platform_local_runtime import _build_minimal_catalog

from knowledge_platform.database import LocalVannaCollectionCandidateRebuilder

ROOT = Path(__file__).resolve().parents[2]


def _port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def _json(port, path, body=None):
    request = Request(f"http://127.0.0.1:{port}{path}",
                      data=json.dumps(body).encode() if body is not None else None,
                      headers={"Content-Type": "application/json"})
    try:
        response = urlopen(request, timeout=10)
    except HTTPError as error:
        response = error
    with response:
        return json.load(response)


@pytest.mark.parametrize("launcher", [False, True], ids=["independent", "launcher"])
def test_independent_database_and_wiki_runtime(tmp_path, launcher):
    pytest.importorskip("asyncpg", reason="real PostgreSQL replay requires the postgres extra")
    binaries = {name: shutil.which(name) for name in ("initdb", "postgres", "psql")}
    if not all(binaries.values()):
        pytest.skip("temporary PostgreSQL binaries are unavailable")
    env = {"PATH": os.environ["PATH"], "LC_ALL": "C"}
    pgroot = tmp_path / "pg"
    subprocess.run([binaries["initdb"], "-D", str(pgroot), "-A", "trust", "--no-locale", "--encoding=UTF8", "-U", "knowledge_test"],
                   check=True, capture_output=True, env=env, timeout=30)
    pgport, port = _port(), _port()
    with (tmp_path / "postgres.log").open("w") as log:
        postgres = subprocess.Popen([binaries["postgres"], "-D", str(pgroot), "-h", "127.0.0.1", "-p", str(pgport),
                                     "-c", "unix_socket_directories=", "-c", "fsync=off"],
                                    env=env, stdout=log, stderr=log)
        try:
            psql = [binaries["psql"], "-h", "127.0.0.1", "-p", str(pgport), "-U", "knowledge_test",
                    "-d", "postgres", "-v", "ON_ERROR_STOP=1", "-At"]
            for _ in range(100):
                probe = subprocess.run(psql + ["-c", "SELECT 1"], env=env, capture_output=True, timeout=5)
                if probe.returncode == 0:
                    break
                assert postgres.poll() is None
                time.sleep(.1)
            else:
                pytest.fail("temporary PostgreSQL failed to start")
            subprocess.run(psql + ["-c", "CREATE TABLE sales(amount integer); INSERT INTO sales VALUES (10),(20); CREATE ROLE knowledge_reader LOGIN; GRANT SELECT ON sales TO knowledge_reader;"],
                           env=env, capture_output=True, check=True, timeout=10)
            _replay(tmp_path, env, pgport, port, launcher)
            result = subprocess.run(psql + ["-c", "SELECT COUNT(*), SUM(amount) FROM sales"],
                                    env=env, capture_output=True, check=True, timeout=10)
            assert result.stdout.strip() == b"2|30"
        finally:
            postgres.terminate()
            postgres.wait(timeout=15)


def _replay(tmp_path, env, pgport, port, launcher):
    collection = tmp_path / "local_collection"
    builder = LocalVannaCollectionCandidateRebuilder(collection_root=collection, collection_name="local_collection")
    builder.begin(package_revision=DIGEST, input_digest=DIGEST)
    builder.add_ddl(source_id="sales", item_id="ddl", content="CREATE TABLE sales (amount integer);")
    builder.add_sql_example(source_id="sales", item_id="sum", question="sales total", sql="SELECT SUM(amount) AS total FROM sales")
    builder.add_sql_example(source_id="sales", item_id="write", question="delete sales", sql="DELETE FROM sales")
    builder.commit()
    config = tmp_path / "database.json"
    config_value = database_config(collection, pgport)
    config_value["source"]["password_env"] = "KNOWLEDGE_DB_TEST_PASSWORD"
    env = {**env, "KNOWLEDGE_DB_TEST_PASSWORD": "fixture-only-password"}
    config.write_text(json.dumps(config_value))
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "local.md").write_text("# Local wiki\ncombined runtime evidence\n")
    catalog = tmp_path / "catalog.sqlite3"
    _build_minimal_catalog(catalog)
    before = hashlib.sha256(catalog.read_bytes()).hexdigest()
    python = os.environ.get("KNOWLEDGE_TEST_PYTHON", sys.executable)
    if "KNOWLEDGE_TEST_PYTHON" not in os.environ:
        env = {**env, "PYTHONPATH": str(ROOT / "backend")}
    ready = tmp_path / "ready.json"
    args = [python, "-m", "knowledge_platform.local", "--catalog", str(catalog), "--wiki-root", str(wiki),
            "--temp-dir", str(tmp_path / "runtime"), "--ready-file", str(ready), "--port", str(port),
            "--database-config", str(config)]
    if launcher:
        webport = _port()
        args = [str(ROOT / "scripts/start-knowledge-local.sh"), "--no-build",
                "--catalog", str(catalog), "--wiki-root", str(wiki), "--database-config", str(config),
                "--api-port", str(port), "--console-port", str(webport)]
    child = subprocess.Popen(args, cwd=tmp_path, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        for _ in range(200):
            if child.poll() is not None:
                diagnostic = subprocess.run([python, "-c",
                    "from pathlib import Path; import sys; from knowledge_platform.local.database import load_database_config, build_database_services; build_database_services(load_database_config(Path(sys.argv[1])), Path(sys.argv[2]))",
                    str(config), str(tmp_path / "runtime" / "knowledge-platform.sqlite3")],
                    env=env, cwd=tmp_path, capture_output=True, timeout=10)
                pytest.fail(child.communicate()[1].decode() + diagnostic.stderr.decode())
            try:
                if _json(port, "/v1/spaces")["status"] == "ok":
                    break
            except OSError:
                time.sleep(.1)
        else:
            pytest.fail("database runtime failed to start")
        if launcher:
            for _ in range(200):
                try:
                    if _json(webport, "/v1/spaces")["status"] == "ok":
                        break
                except OSError:
                    time.sleep(.1)
            else:
                pytest.fail("product Web BFF failed to start")
            # Run all database and Wiki requests through the product BFF.
            port = webport
        else:
            assert json.loads(ready.read_text())["database_configured"] is True
        scope = {"space_id": "space_kb_default", "dataset_id": "database_sales"}
        schema = _json(port, "/v1/database/schema?space_id=space_kb_default&dataset_id=database_sales")
        assert schema["status"] == "ok"
        assert schema["data"]["tables"][0]["columns"] == ["amount"]
        collections = _json(port, "/v1/collections?space_id=space_kb_default")
        assert "database_sales" in json.dumps(collections)
        for transport in ("rest", "mcp"):
            def call(name, path, body):
                if transport == "rest":
                    return _json(port, path, body)
                result = _json(port, "/mcp", {"jsonrpc": "2.0", "id": name, "method": "tools/call",
                                                "params": {"name": name, "arguments": body}})
                return result["result"]["structuredContent"]
            plan = call("database_nl2sql", "/v1/database/nl2sql", {**scope, "question": "sales total"})
            assert plan["status"] == "ok", plan
            plan_id = plan["data"]["query_plan"]["query_plan_id"]
            execution = {"space_id": scope["space_id"], "expected_sql_hash": plan["data"]["sql_hash"], "page_size": 20}
            path = f"/v1/database/query-plans/{plan_id}:execute"
            result = call("database_execute_readonly", path, {**execution, "query_plan_id": plan_id})
            assert result["status"] == "ok", result
            assert result["data"]["rows"] == [{"total": 30}]
            rejected = call("database_execute_readonly", path, {**execution, "query_plan_id": plan_id, "expected_sql_hash": "sha256:" + "b" * 64})
            assert rejected["status"] == "error"
        for body in ({**scope, "question": "delete sales"}, {**scope, "question": "sales total", "space_id": "space_other"}):
            assert _json(port, "/v1/database/nl2sql", body)["status"] == "error"
        assets = _json(port, "/v1/assets?space_id=space_kb_default")["data"]["assets"]
        asset = next(a for a in assets if a["kind"] == "wiki_page")
        read = _json(port, f"/v1/assets/{asset['id']}:read", {"end": 4096})
        assert read["status"] == "ok"
        assert b"combined runtime evidence" in base64.b64decode(read["data"]["content_base64"])
        assert str(tmp_path) not in json.dumps([schema, plan, result, read])
        assert "fixture-only-password" not in json.dumps([schema, plan, result, read])
    finally:
        child.terminate()
        child.communicate(timeout=10)
    assert not ready.exists()
    assert hashlib.sha256(catalog.read_bytes()).hexdigest() == before
