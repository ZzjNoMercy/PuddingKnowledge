import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from knowledge_platform.connector_sync.feishu_api import FeishuApiError, FeishuApiClient, HttpResponse


class Transport:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def request(self, method, url, *, headers, body, timeout_seconds):
        self.calls.append((method, url, headers, timeout_seconds))
        return self.response


def test_media_uses_fixed_origin_and_strict_binary_response():
    async def run():
        transport = Transport(HttpResponse(200, {"content-type": "image/png", "content-length": "3"}, b"PNG"))
        client = FeishuApiClient("token_media", endpoint="http://127.0.0.1:9911", transport=transport)
        body, content_type, _ = await client.download_media_file(file_token="file_1")
        assert body == b"PNG" and content_type == "image/png"
        method, url, headers, _ = transport.calls[0]
        assert method == "GET" and url == "http://127.0.0.1:9911/open-apis/drive/v1/medias/file_1/download"
        assert headers["Authorization"] == "Bearer token_media"
        assert headers["Accept"] == "application/octet-stream"
        with pytest.raises(ValueError):
            await client.download_media_file(file_token="https://evil.example/file")
    asyncio.run(run())


@pytest.mark.parametrize("response", [
    HttpResponse(200, {"content-type": "application/json", "content-length": "35"}, b'{"code":999,"msg":"not binary"}'),
    HttpResponse(200, {"content-length": "35"}, b'{"code":999,"msg":"not binary"}'),
    HttpResponse(500, {"content-type": "application/json"}, b'{"code":999,"msg":"provider failure"}'),
    HttpResponse(302, {"location": "https://evil.example/"}, b""),
    HttpResponse(200, {"content-type": "image/png", "content-length": "8"}, b"short"),
])
def test_media_rejects_json_errors_redirects_and_truncated_body(response):
    async def run():
        client = FeishuApiClient("token_media", endpoint="http://127.0.0.1:9911", transport=Transport(response))
        with pytest.raises(FeishuApiError):
            await client.download_media_file(file_token="file_1")
    asyncio.run(run())


def test_media_enforces_per_file_and_cumulative_budgets_without_partial_result():
    class SequenceTransport:
        def __init__(self):
            self.calls = []

        async def request(self, method, url, *, headers, body, timeout_seconds):
            self.calls.append(url)
            return HttpResponse(200, {"content-type": "image/png"}, b"1234")

    async def run():
        transport = SequenceTransport()
        client = FeishuApiClient("token_media", endpoint="http://127.0.0.1:9911", transport=transport)
        result = await client.download_media_assets(file_tokens=["file_1", "file_1"], max_bytes_each=4, max_total_bytes=4)
        assert list(result) == ["file_1"] and len(transport.calls) == 1
        with pytest.raises(FeishuApiError, match="上限"):
            await client.download_media_assets(file_tokens=["file_1", "file_2"], max_bytes_each=4, max_total_bytes=6)
    asyncio.run(run())


def test_real_http_media_endpoint_does_not_follow_redirect_or_accept_json():
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append((self.path, self.headers.get("Authorization")))
            if self.path.endswith("/redirect/download"):
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1:1/steal")
                self.end_headers()
                return
            body = b"{\"code\":0,\"msg\":\"error\"}"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        async def run():
            client = FeishuApiClient("token_media", endpoint=f"http://127.0.0.1:{server.server_port}")
            with pytest.raises(FeishuApiError):
                await client.download_media_file(file_token="redirect")
            with pytest.raises(FeishuApiError):
                await client.download_media_file(file_token="json")
        asyncio.run(run())
        assert all(token == "Bearer token_media" for _, token in calls)
        assert len(calls) == 2
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_real_http_binary_timeout_is_overall_and_chunked_body_is_supported():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.endswith('/slow/download'):
                self.send_response(200)
                self.send_header('Content-Type', 'application/octet-stream')
                self.send_header('Transfer-Encoding', 'chunked')
                self.end_headers()
                for chunk in (b'1', b'2', b'3'):
                    self.wfile.write(f'{len(chunk):X}\r\n'.encode() + chunk + b'\r\n')
                    self.wfile.flush()
                    time.sleep(0.15)
                self.wfile.write(b'0\r\n\r\n')
                return
            self.send_response(200)
            self.send_header('Content-Type', 'application/octet-stream')
            self.send_header('Transfer-Encoding', 'chunked')
            self.end_headers()
            body = b'chunked-ok'
            self.wfile.write(f'{len(body):X}\r\n'.encode() + body + b'\r\n0\r\n\r\n')

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        async def run():
            client = FeishuApiClient('token_media', endpoint=f'http://127.0.0.1:{server.server_port}', timeout_seconds=0.1)
            started = time.monotonic()
            with pytest.raises(FeishuApiError, match='超时'):
                await client.download_media_file(file_token='slow')
            assert time.monotonic() - started < 0.8
            client = FeishuApiClient('token_media', endpoint=f'http://127.0.0.1:{server.server_port}')
            body, _, _ = await client.download_media_file(file_token='ok')
            assert body == b'chunked-ok'
        asyncio.run(run())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
