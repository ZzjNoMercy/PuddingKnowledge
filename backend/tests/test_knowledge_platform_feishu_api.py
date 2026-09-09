from __future__ import annotations

import json
import gzip
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock

import pytest

from knowledge_platform.connector_sync.feishu_api import FeishuApiClient, FeishuApiError, HttpResponse


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def request(self, method, url, *, headers, body, timeout_seconds):
        self.calls.append((method, url, dict(headers), body, timeout_seconds))
        return self.responses.pop(0)


def response(payload, status=200):
    return HttpResponse(status, {"content-type": "application/json"}, json.dumps(payload).encode())


@pytest.fixture
def loopback_server():
    class Handler(BaseHTTPRequestHandler):
        calls = []
        lock = Lock()

        def do_GET(self):
            with self.lock:
                self.calls.append(self.path)
            if self.path.startswith("/open-apis/wiki/v2/spaces"):
                body = ({"code": 0, "data": {"items": [{"space_id": "s2"}], "has_more": False}}
                        if "page_token=p1" in self.path else
                        {"code": 0, "data": {"items": [{"space_id": "s"}], "has_more": True, "page_token": "p1"}})
                encoded = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)
                return
            if self.path.endswith("/redirect"):
                self.send_response(302)
                self.send_header("Location", "/counted")
                self.end_headers()
                return
            if self.path.endswith("/counted"):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")
                return
            if self.path.endswith("/large"):
                encoded = gzip.compress(json.dumps({"code": 0, "data": {"blob": "x" * (8 * 1024 * 1024 + 1)}}).encode())
                self.send_response(200)
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)
                return
            self.send_response(404)
            self.end_headers()

        def log_message(self, *_args):
            return

    try:
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    except PermissionError:
        pytest.skip("loopback binding is unavailable in this sandbox")
    try:
        yield server, Handler
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.asyncio
async def test_real_loopback_pagination(loopback_server):
    server, _handler = loopback_server
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = FeishuApiClient("secret-token", endpoint=f"http://127.0.0.1:{server.server_port}")
    assert [x["space_id"] for x in await client.list_spaces()] == ["s", "s2"]
    server.shutdown()
    thread.join()


@pytest.mark.asyncio
async def test_loopback_no_redirect_and_gzip_limit(loopback_server):
    server, handler = loopback_server
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = FeishuApiClient("secret-token", endpoint=f"http://127.0.0.1:{server.server_port}")
    with pytest.raises(FeishuApiError):
        await client._request("GET", "/open-apis/redirect")
    assert "/counted" not in handler.calls
    with pytest.raises(FeishuApiError, match="大小上限"):
        await client._request("GET", "/open-apis/large")
    server.shutdown()
    thread.join()


@pytest.mark.asyncio
async def test_malformed_pages_and_error_message_are_safe():
    for payload in (
        {"code": 0},
        {"code": 0, "data": {"items": [{"ok": 1}], "has_more": "yes"}},
        {"code": 0, "data": {"items": [{"ok": 1}, "bad"], "has_more": False}},
    ):
        client = FeishuApiClient("token", endpoint="http://127.0.0.1:18080", transport=FakeTransport([response(payload)]))
        with pytest.raises(FeishuApiError):
            await client.list_spaces()
    payload = {"code": 123, "msg": "token=secret-token"}
    client = FeishuApiClient("token", endpoint="http://127.0.0.1:18080", transport=FakeTransport([response(payload)]))
    with pytest.raises(FeishuApiError) as caught:
        await client.list_spaces()
    assert "secret-token" not in str(caught.value)


@pytest.mark.asyncio
async def test_spaces_paginate_and_bfs_nodes():
    transport = FakeTransport([
        response({"code": 0, "data": {"items": [{"space_id": "s"}], "has_more": True, "page_token": "p1"}}),
        response({"code": 0, "data": {"items": [{"space_id": "s2"}], "has_more": False}}),
    ])
    client = FeishuApiClient("secret-token", endpoint="http://127.0.0.1:18080", transport=transport)
    assert [x["space_id"] for x in await client.list_spaces()] == ["s", "s2"]
    assert transport.calls[0][2]["Authorization"] == "Bearer secret-token"


def test_endpoint_and_token_are_restricted():
    with pytest.raises(ValueError):
        FeishuApiClient("token", endpoint="http://example.test")
    with pytest.raises(ValueError):
        FeishuApiClient("token", endpoint="https://open.feishu.cn/path")
    with pytest.raises(ValueError):
        FeishuApiClient("bad token")


@pytest.mark.asyncio
async def test_repeated_page_token_is_rejected_and_binary_is_bounded():
    transport = FakeTransport([
        response({"code": 0, "data": {"items": [], "has_more": True, "page_token": "same"}}),
        response({"code": 0, "data": {"items": [], "has_more": True, "page_token": "same"}}),
    ])
    client = FeishuApiClient("token", endpoint="http://localhost:18080", transport=transport)
    with pytest.raises(FeishuApiError, match="游标异常"):
        await client.list_spaces()

    binary = FakeTransport([HttpResponse(200, {"Content-Type": "application/pdf"}, b"12345")])
    client = FeishuApiClient("token", endpoint="http://localhost:18080", transport=binary)
    with pytest.raises(FeishuApiError, match="大小上限"):
        await client.download_drive_file(file_token="file_1", max_bytes=4)

@pytest.mark.asyncio
async def test_many_individually_bounded_pages_have_a_total_memory_bound():
    transport=FakeTransport([response({'code':0,'data':{'items':[{'title':'x'*(7*1024*1024)}],
        'has_more':True,'page_token':str(index)}}) for index in range(6)])
    client=FeishuApiClient('token',transport=transport)
    with pytest.raises(FeishuApiError,match='累计响应'):
        await client.list_spaces()
    assert len(transport.calls)==5


@pytest.mark.asyncio
async def test_bitable_record_page_validates_raw_continuation():
    for page in (
        {"items":[],"has_more":True,"page_token":{"opaque":"x"}},
        {"items":[],"has_more":False,"page_token":[]},
        {"items":[],"has_more":True,"page_token":""},
        {"items":[],"has_more":True,"page_token":"repeat"},
        {"items":[{},{}],"has_more":False},
    ):
        client=FeishuApiClient("token",transport=FakeTransport([response({"code":0,"data":page})]))
        with pytest.raises(FeishuApiError):
            await client.list_bitable_records_page(app_token="app",table_id="tbl",page_size=1,page_token="repeat")
