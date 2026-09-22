"""Real Feishu Bitable wire protocol through an independently started runtime."""
import json
import os
import asyncio
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from urllib.parse import urlsplit, parse_qs

import pytest

from test_knowledge_platform_local_runtime import _build_minimal_catalog


@pytest.fixture
def remote():
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            assert self.path == "/open-apis/auth/v3/tenant_access_token/internal"
            assert body == {"app_id": "cli_bitable", "app_secret": "BITABLE_SECRET_349"}
            assert self.headers.get("Authorization") is None
            calls.append(("auth", self.path))
            self._send({"code": 0, "tenant_access_token": "bitable-token", "expire": 7200})

        def do_GET(self):
            assert self.headers.get("Authorization") == "Bearer bitable-token"
            calls.append(("get", self.path))
            if self.path.startswith("/open-apis/bitable/v1/apps/app_bitable/tables/tbl_cars/records"):
                params=parse_qs(urlsplit(self.path).query)
                assert params["view_id"]==["vew_cars"] and params["page_size"]==["1"]
                assert json.loads(params["field_names"][0])==["Count","Name"]
                self._send({"code": 0, "data": {"items": [
                    {"record_id": "rec_1", "fields": {"Name": "ROW-CANARY", "Count": 3}},
                ], "has_more": True, "page_token": "provider-next", "total": 2}})
            elif self.path.startswith("/open-apis/bitable/v1/apps/app_bitable/tables/tbl_cars/fields"):
                self._send({"code": 0, "data": {"items": [
                    {"field_id": "fld_name", "field_name": "Name", "type": 1,
                     "property": {"ui_type": "Text"}, "is_primary": True},
                    {"field_id": "fld_count", "field_name": "Count", "type": 2,
                     "property": {"ui_type": "Number"}},
                ], "has_more": False}})
            elif self.path.startswith("/open-apis/bitable/v1/apps/app_bitable/tables"):
                self._send({"code": 0, "data": {"items": [
                    {"table_id": "tbl_cars", "name": "Cars"},
                    {"table_id": "tbl_other", "name": "Other"},
                ], "has_more": False}})
            else:
                self.send_error(404)

        def _send(self, value):
            payload = json.dumps(value).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_real_process_bitable_schema_live_query_policy_restart_and_cursor_fence(tmp_path, remote):
    endpoint, calls = remote
    catalog = tmp_path / "seed.db"
    _build_minimal_catalog(catalog)
    original_catalog = catalog.read_bytes()
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "seed.md").write_text("# Bitable runtime fixture\n")
    config = tmp_path / "feishu.json"
    config.write_text(json.dumps({"version": 1, "sources": [{
        "id": "bitable_fixture", "name": "Bitable Fixture",
        "selection": {"kind": "bitable", "root": "app_bitable", "wiki_space": ""},
        "app_id": "cli_bitable", "app_secret_env": "FEISHU_BITABLE_SECRET", "endpoint": endpoint,
        "bitable": {"tables": [{"table_id": "tbl_cars", "view_id": "vew_cars"}], "relations": []},
    }]}))
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    env = dict(os.environ, FEISHU_BITABLE_SECRET="BITABLE_SECRET_349")
    if os.environ.get("KNOWLEDGE_TEST_INSTALLED") == "1":
        env.pop("PYTHONPATH", None)
        env.pop("PYTHONHOME", None)
    else:
        env["PYTHONPATH"] = str(Path(__file__).parents[1])

    def request(path, body=None, method="GET"):
        data = None if body is None else json.dumps(body).encode()
        req = Request(f"http://127.0.0.1:{port}{path}", data=data, method=method,
                      headers={"Content-Type": "application/json"})
        try:
            with urlopen(req, timeout=20) as response:
                return json.load(response)
        except HTTPError as error:
            detail = error.read().decode()
            if proc is not None:
                proc.terminate()
                stderr = proc.communicate(timeout=5)[1].decode()
                detail += f"\nprocess stderr:\n{stderr}"
            raise AssertionError(f"runtime HTTP {error.code}: {detail}") from error

    state = tmp_path / "state"
    proc = None
    try:
        for turn in range(2):
            ready = tmp_path / f"ready-{turn}.json"
            proc = subprocess.Popen([
                sys.executable, "-m", "knowledge_platform.local", "--catalog", str(catalog),
                "--wiki-root", str(wiki),
                "--state-dir", str(state), "--temp-dir", str(tmp_path / f"temp-{turn}"),
                "--ready-file", str(ready), "--port", str(port), "--feishu-config", str(config),
                "--console-origin", "http://127.0.0.1:9999",
            ], cwd=tmp_path, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            for _ in range(200):
                if proc.poll() is not None:
                    pytest.fail(proc.stderr.read().decode())
                if ready.exists():
                    break
                time.sleep(.05)
            else:
                pytest.fail("Bitable runtime did not become ready")

            preflight=Request(f"http://127.0.0.1:{port}/v1/sources/bitable_fixture/bitable/policy", method="OPTIONS", headers={"Origin":"http://127.0.0.1:9999","Access-Control-Request-Method":"PUT","Access-Control-Request-Headers":"content-type"})
            with urlopen(preflight,timeout=5) as response:
                assert response.status==200
                assert response.headers['Access-Control-Allow-Origin']=='http://127.0.0.1:9999'
                assert 'PUT' in response.headers['Access-Control-Allow-Methods']
            foreign=Request(preflight.full_url,method="OPTIONS",headers={"Origin":"http://127.0.0.1:9998","Access-Control-Request-Method":"PUT"})
            with pytest.raises(HTTPError) as rejected:urlopen(foreign,timeout=5)
            assert rejected.value.code==400
            if turn == 0:
                sync = request("/v1/sources/bitable_fixture:sync", {"idempotency_key": "bitable-schema", "mode": "full"}, "POST")
                assert sync["status"] == "ok", sync
                assert sync["data"]["sync"]["linked"] == 1
                assert not any("/records" in path for _,path in calls)
                import sqlite3
                with sqlite3.connect(state / "catalog.sqlite3") as connection:
                    items = connection.execute("SELECT external_type, status FROM knowledge_source_items WHERE connector_id='bitable_fixture'").fetchall()
                    assets = connection.execute("SELECT kind, metadata_json FROM knowledge_assets WHERE source_type='feishu'").fetchall()
                assert items == [("bitable_table", "linked")]
                assert len(assets) == 1 and assets[0][0] == "table_schema"
                assert "ROW-CANARY" not in (state / "catalog.sqlite3").read_bytes().decode("utf-8", "ignore")
                schema = request("/v1/sources/bitable_fixture/bitable/tables/tbl_cars/schema")
                assert schema["status"] == "ok", schema
                revision = schema["data"]["schema_revision"]
                assert schema["data"]["row_storage"] is False
                query = request("/v1/sources/bitable_fixture/bitable/query", {
                    "table_id": "tbl_cars", "schema_revision": revision,
                    "field_names": ["Name", "Count"], "page_size": 1, "cursor": "",
                }, "POST")
                assert query["status"] == "ok", query
                assert query["data"]["records"][0]["fields"]["Name"] == "ROW-CANARY"
                assert query["data"]["has_more"] is True and query["data"]["next_cursor"]
                old_cursor = query["data"]["next_cursor"]

                async def generic_mcp_query():
                    import httpx
                    from langchain_mcp_adapters.client import MultiServerMCPClient
                    def factory(*, headers=None, timeout=None, auth=None):
                        return httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}",
                            transport=httpx.AsyncHTTPTransport(), headers=headers, timeout=timeout, auth=auth)
                    client = MultiServerMCPClient({"platform": {
                        "transport": "streamable-http", "url": f"http://127.0.0.1:{port}/mcp",
                        "httpx_client_factory": factory}}, tool_name_prefix=True)
                    tools = await client.get_tools(server_name="platform")
                    query_tool = next(tool for tool in tools if tool.name == "platform_feishu_bitable_query")
                    return await query_tool.ainvoke({"source_id": "bitable_fixture", "table_id": "tbl_cars",
                        "schema_revision": revision, "field_names": ["Name", "Count"], "page_size": 1, "cursor": ""})

                mcp_query = asyncio.run(generic_mcp_query())
                assert "ROW-CANARY" in str(mcp_query)
                assert "ROW-CANARY" not in (state / "catalog.sqlite3").read_bytes().decode("utf-8", "ignore")
                policy = request("/v1/sources/bitable_fixture/bitable/policy")
                shrunk = request("/v1/sources/bitable_fixture/bitable/policy", {
                    "expected_revision": policy["data"]["policy_revision"],
                    "policy": {"tables": [], "relations": []},
                }, "PUT")
                assert shrunk["status"] == "ok" and shrunk["data"]["tables"] == []
                stale = request("/v1/sources/bitable_fixture/bitable/query", {
                    "table_id": "tbl_cars", "schema_revision": revision,
                    "field_names": ["Name"], "page_size": 1, "cursor": old_cursor,
                }, "POST")
                assert stale["status"] == "error"
                assert "ROW-CANARY" not in (state / "catalog.sqlite3").read_bytes().decode("utf-8", "ignore")
                proc.terminate(); proc.wait(timeout=10); proc = None
                env.pop("FEISHU_BITABLE_SECRET", None)
            else:
                persisted = request("/v1/sources/bitable_fixture/bitable/policy")
                assert persisted["status"] == "ok"
                assert persisted["data"]["tables"] == []
                assert persisted["data"]["row_storage"] is False
                proc.terminate(); proc.wait(timeout=10); proc = None
        assert sum(path.endswith("/records") or "/records?" in path for _, path in calls) == 2
        assert catalog.read_bytes() == original_catalog
        assert all(b"ROW-CANARY" not in path.read_bytes() for path in state.rglob("*") if path.is_file())
        assert b"BITABLE_SECRET_349" not in (state / "catalog.sqlite3").read_bytes()
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill(); proc.wait(timeout=5)
