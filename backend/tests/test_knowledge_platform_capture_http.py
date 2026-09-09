from __future__ import annotations

import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from knowledge_platform.capture.http import (
    MAX_RESPONSE_BYTES,
    UnsafePublicURL,
    fetch_public_url,
)


class _Server:
    def __init__(self, routes):
        self.routes = routes
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                status, headers, body = owner.routes.get(self.path, (404, {}, b"missing"))
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def origin(self):
        return f"http://127.0.0.1:{self.httpd.server_port}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


def test_loopback_fixture_requires_exact_allowed_origin_and_follows_redirect():
    with _Server({
        "/start": (302, {"Location": "/final"}, b""),
        "/final": (200, {"Content-Type": "text/plain; charset=utf-8"}, b"captured"),
    }) as server:
        with pytest.raises(UnsafePublicURL):
            fetch_public_url(server.origin + "/start")
        result = fetch_public_url(server.origin + "/start", allowed_origins=(server.origin,))
        assert result.url == server.origin + "/final"
        assert result.content_type == "text/plain"
        assert result.body == b"captured"


def test_connects_to_resolved_ip_but_preserves_original_host(monkeypatch):
    observed = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            observed.append(self.headers.get("Host"))
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"pinned")

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_port
        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            lambda *_args, **_kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))],
        )
        origin = f"http://public.example:{port}"
        result = fetch_public_url(origin + "/page", allowed_origins=(origin,))
        assert result.body == b"pinned"
        assert observed == [f"public.example:{port}"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_mixed_public_and_private_dns_is_rejected_without_explicit_origin(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80)),
        ],
    )
    with pytest.raises(UnsafePublicURL, match="non-public"):
        fetch_public_url("http://mixed.example/")


def test_redirect_target_is_revalidated_and_redirect_loop_is_bounded(monkeypatch):
    calls = []

    def fake_request(**kwargs):
        calls.append(kwargs["request_url"])
        return type("Response", (), {
            "status": 302,
            "headers": {"location": "http://127.0.0.1:80/next"},
            "body": b"",
        })()

    def resolve(hostname, _port, **_kwargs):
        if hostname != "public.example":
            raise UnsafePublicURL("target resolves to a non-public address")
        return ("93.184.216.34",)

    monkeypatch.setattr("knowledge_platform.capture.http._resolve_public_addresses", resolve)
    monkeypatch.setattr("knowledge_platform.capture.http._request_once", fake_request)
    with pytest.raises(UnsafePublicURL):
        fetch_public_url("http://public.example/")
    assert len(calls) == 1


def test_oversized_response_is_rejected_before_return(monkeypatch):
    with _Server({
        "/large": (200, {"Content-Type": "text/plain", "Content-Length": str(MAX_RESPONSE_BYTES + 1)}, b"x" * (MAX_RESPONSE_BYTES + 1)),
    }) as server:
        with pytest.raises(ValueError, match="5 MiB"):
            fetch_public_url(server.origin + "/large", allowed_origins=(server.origin,))


@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "http://user:password@example.com/",
    "http://example.com/\nforged",
    "http://example.com:8080/",
])
def test_url_boundary_rejections(url):
    with pytest.raises((UnsafePublicURL, ValueError)):
        fetch_public_url(url)
