import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from knowledge_platform.connector_sync.feishu_api import HttpResponse
from knowledge_platform.connector_sync.feishu_oauth import FeishuOAuthError, OAuthClient, TokenSet
from knowledge_platform.catalog.metadata import KNOWLEDGE_METADATA
from knowledge_platform.catalog.models import KnowledgeConnector, KnowledgeCredentialGrant, KnowledgeOAuthSession
from knowledge_platform.local.feishu_oauth import LocalFeishuOAuth, OAuthStateError, _binding
from knowledge_platform.local.vault import LocalCredentialStore


class Transport:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    async def request(self, method, url, *, headers, body, timeout_seconds):
        self.calls.append((method, url, headers, json.loads(body) if body else None))
        return HttpResponse(200, {"content-type": "application/json"}, json.dumps(self.payload).encode())


def test_authorization_url_uses_official_accounts_and_pkce():
    url = OAuthClient().authorization_url("cli_test", "https://example.test/callback", ["wiki:wiki:readonly"], "s" * 16, "c" * 43)
    assert url.startswith("https://accounts.feishu.cn/open-apis/authen/v1/authorize?")
    assert "code_challenge_method=S256" in url


def test_exchange_strictly_parses_token_and_actual_scopes():
    async def run():
        transport = Transport({"code": 0, "access_token": "access", "refresh_token": "refresh",
                               "expires_in": 3600, "refresh_expires_in": 86400, "scope": "wiki:wiki:readonly"})
        result = await OAuthClient(endpoint="http://127.0.0.1:9911", transport=transport).exchange(
            "cli_test", "secret", "code", "http://127.0.0.1/callback", "v" * 43)
        assert result == TokenSet("access", "refresh", 3600, 86400, ("wiki:wiki:readonly",))
        assert transport.calls[0][3]["grant_type"] == "authorization_code"
    asyncio.run(run())


def test_refresh_scope_absence_is_explicit_none_and_errors_are_safe():
    async def run():
        transport = Transport({"code": 0, "access_token": "access", "refresh_token": "refresh",
                               "expires_in": 3600, "refresh_expires_in": 86400})
        result = await OAuthClient(endpoint="http://127.0.0.1:9911", transport=transport).refresh("cli", "secret", "old")
        assert result.scopes is None

        bad = Transport({"code": 0, "access_token": "access", "refresh_token": "refresh", "expires_in": 3600,
                         "refresh_expires_in": 86400})
        with pytest.raises(FeishuOAuthError) as exc:
            await OAuthClient(endpoint="http://127.0.0.1:9911", transport=bad).exchange(
                "cli", "super-secret", "code", "http://127.0.0.1/callback", "v" * 43)
        assert "super-secret" not in str(exc.value)
    asyncio.run(run())


def test_expiry_alias_conflict_and_non_string_values_fail_closed():
    async def run():
        payload = {"code": 0, "access_token": "a", "refresh_token": "r", "expires_in": 10,
                   "refresh_expires_in": 20, "refresh_token_expires_in": 21, "scope": "x"}
        with pytest.raises(FeishuOAuthError):
            await OAuthClient(endpoint="http://127.0.0.1:9911", transport=Transport(payload)).exchange(
                "cli", "secret", "code", "http://127.0.0.1/callback", "v" * 43)
        malformed = dict(payload)
        malformed.pop("refresh_token_expires_in")
        malformed["access_token"] = True
        with pytest.raises(FeishuOAuthError):
            await OAuthClient(endpoint="http://127.0.0.1:9911", transport=Transport(malformed)).exchange(
                "cli", "secret", "code", "http://127.0.0.1/callback", "v" * 43)
    asyncio.run(run())


def test_redirect_and_non_loopback_endpoints_are_rejected():
    with pytest.raises(FeishuOAuthError):
        OAuthClient().authorization_url("cli", "http://evil.example/cb", ["a"], "s" * 16, "c" * 43)
    with pytest.raises(ValueError):
        OAuthClient(endpoint="http://evil.example")


def test_real_threading_http_server_token_userinfo_and_no_redirect():
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            request=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            assert request['grant_type']=='authorization_code' and request['code_verifier']=='v'*43
            body = {"code": 0, "access_token": "a", "refresh_token": "r", "expires_in": 10,
                    "refresh_token_expires_in": 20, "scope": "wiki:wiki:readonly"}
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length",str(len(json.dumps(body).encode())))
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

        def do_GET(self):
            if self.path.endswith("user_info"):
                body = {"code": 0, "data": {"open_id": "ou_test"}}
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(body).encode())
            else:
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1/")
                self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = OAuthClient(endpoint=f"http://127.0.0.1:{server.server_port}")
        async def run():
            tokens = await client.exchange("cli", "secret", "code", "http://127.0.0.1/callback", "v" * 43)
            assert tokens.scopes == ("wiki:wiki:readonly",)
            assert (await client.userinfo("a"))["open_id"] == "ou_test"
            with pytest.raises(FeishuOAuthError):
                await client._request("GET", "/redirect")
        asyncio.run(run())
    finally:
        server.shutdown()
        server.server_close()


