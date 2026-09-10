import asyncio
import io
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time
import zipfile
import httpx

import pytest

from knowledge_platform.parsers.mineru import MinerUClient, MinerUError, ParseLimits, rewrite_media


class Response:
    def __init__(self, data): self.data = data
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def read(self, limit=-1): return self.data if limit < 0 else self.data[:limit]


class Opener:
    def __init__(self, data): self.data = data; self.request = None
    def open(self, request, timeout): self.request = request; return Response(self.data)


def archive(items):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as zf:
        for name, body in items: zf.writestr(name, body)
    return output.getvalue()


def run(client, payload): return asyncio.run(client.parse_pdf(b"%PDF", "a.pdf"))


def transport_for(data, capture=None, status=200, headers=None):
    def handler(request):
        if capture is not None: capture.append(request)
        return httpx.Response(status, content=data, headers=headers or {"content-type": "application/zip"})
    return httpx.MockTransport(handler)


def test_zip_returns_markdown_and_keeps_media_paths_and_multipart_files():
    requests = []
    result = run(MinerUClient("http://127.0.0.1:8000", transport=transport_for(archive([("nested/doc.md", b"# hi\n![x](assets/pic.png)"), ("nested/assets/pic.png", b"PNG")]), requests)), None)
    assert result.markdown == b"# hi\n![x](nested/assets/pic.png)"
    assert result.assets[0].relative_path == "nested/assets/pic.png"
    assert result.version == "local-http-v1"
    assert b'name="files"' in requests[0].content
    assert b'name="return_images"' in requests[0].content
    assert b'name="response_format_zip"' in requests[0].content
    assert result.parser_id == "mineru"


def test_zip_ignores_normal_directory_entries():
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as zf:
        zf.writestr("images/", b"")
        zf.writestr("full.md", b"ok")
    result = run(MinerUClient("http://127.0.0.1:8000", transport=transport_for(output.getvalue())), None)
    assert result.markdown == b"ok" and result.assets == ()


@pytest.mark.parametrize("name", ["../evil.md", "/evil.md", "nested\\evil.md"])
def test_zip_rejects_unsafe_paths(name):
    with pytest.raises(MinerUError): run(MinerUClient("http://localhost:8000", transport=transport_for(archive([(name, b"x")]))), None)


def test_zip_rejects_ambiguous_markdown_and_missing_media():
    with pytest.raises(MinerUError, match="exactly one"):
        run(MinerUClient("http://127.0.0.1:8000", transport=transport_for(archive([("a.md", b"a"), ("b.md", b"b")]))), None)
    with pytest.raises(MinerUError, match="missing media"):
        run(MinerUClient("http://127.0.0.1:8000", transport=transport_for(archive([("a.md", b"![x](missing.png)")]))), None)


def test_endpoint_is_loopback_only_and_json_requires_explicit_markdown():
    with pytest.raises(ValueError): MinerUClient("http://example.com:8000")
    transport = transport_for(b'{"result":{"markdown":"guess"}}', headers={"content-type": "application/json"})
    with pytest.raises(MinerUError, match="explicit markdown"):
        run(MinerUClient("http://[::1]:8000", transport=transport), None)


def test_root_markdown_and_external_media_are_checked():
    result = run(MinerUClient("http://127.0.0.1:8000", transport=transport_for(archive([("full.md", b"ok")]))), None)
    assert result.markdown == b"ok"
    with pytest.raises(MinerUError, match="external"):
        run(MinerUClient("http://127.0.0.1:8000", transport=transport_for(archive([("full.md", b"![x](https://example.com/a.png)")]))), None)


def test_real_zip_title_angle_and_reference_images_are_staged_and_rewritten():
    markdown = b'![x](<images/a.png> "title")\n![y][pic]\n\n[pic]: images/a.png'
    result = run(MinerUClient("http://127.0.0.1:8000", transport=transport_for(archive([("full.md", markdown), ("images/a.png", b"x")]))), None)
    assert result.markdown.count(b"images/a.png") == 2
    assert len(result.assets) == 1


def test_missing_shortcut_definition_is_rejected_during_parse():
    with pytest.raises(MinerUError, match="definition"):
        run(MinerUClient("http://127.0.0.1:8000", transport=transport_for(archive([("full.md", b"![x][missing]")]))), None)


def test_rewrite_media_preserves_inline_title_angle_html_and_reference_forms():
    source = b'![a](<old/a.png> "title")\n<img src="old/a.png">\n![b][pic]\n[pic]: old/a.png\n'
    rewritten = rewrite_media(source, {"old/a.png": "images/a.png"})
    assert b"<images/a.png> \"title\"" in rewritten
    assert b'src="images/a.png"' in rewritten
    assert b"[pic]: images/a.png" in rewritten


def test_real_loopback_protocol_timeout_truncation_and_http_error():
    payload = archive([("full.md", b"# ok")])
    class Handler(BaseHTTPRequestHandler):
        mode = "ok"
        def do_POST(self):
            length = int(self.headers.get("content-length", "0")); body = self.rfile.read(length)
            if self.mode == "slow": time.sleep(0.2)
            if self.mode == "error": self.send_response(503); self.end_headers(); return
            self.send_response(200); self.send_header("Content-Type", "application/zip"); self.send_header("Content-Length", str(len(payload) + (5 if self.mode == "truncate" else 0))); self.end_headers(); self.wfile.write(payload)
        def log_message(self, *_args): pass
    try:
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    except PermissionError:
        pytest.skip("sandbox does not permit loopback listeners")
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        client = MinerUClient(f"http://127.0.0.1:{server.server_port}", timeout=0.05)
        Handler.mode = "slow"
        started = time.monotonic()
        with pytest.raises(MinerUError, match="timed out"): run(client, None)
        assert time.monotonic() - started < 0.18
        Handler.mode = "ok"
        with pytest.raises(MinerUError, match="exceeds size limit"):
            run(MinerUClient(f"http://127.0.0.1:{server.server_port}", limits=ParseLimits(max_response_bytes=1)), None)
        Handler.mode = "truncate"
        with pytest.raises(MinerUError, match="truncated"):
            run(MinerUClient(f"http://127.0.0.1:{server.server_port}"), None)
        Handler.mode = "error"
        with pytest.raises(MinerUError, match="503"): run(MinerUClient(f"http://127.0.0.1:{server.server_port}"), None)
    finally:
        server.shutdown(); server.server_close()
