"""Stateless Feishu user OAuth HTTP protocol client.

This module intentionally has no persistence or ORM dependency.  Callers own
state/PKCE storage and must treat :class:`TokenSet` as secret material.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Mapping
from urllib.parse import urlencode, urlsplit

from .feishu_api import (
    DEFAULT_ENDPOINT,
    AsyncHttpTransport,
    FeishuApiError,
    HttpResponse,
    _Urllib3Transport,
    _endpoint,
)


AUTHORIZE_PATH = "/open-apis/authen/v1/authorize"
TOKEN_PATH = "/open-apis/authen/v2/oauth/token"
USERINFO_PATH = "/open-apis/authen/v1/user_info"
_TOKEN_RE = re.compile(r"^[A-Za-z0-9._~+/=-]{1,4096}$")
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_MAX_EXPIRE_SECONDS = 10 * 365 * 24 * 60 * 60


class FeishuOAuthError(RuntimeError):
    """Safe OAuth error; provider response bodies and credentials are omitted."""

    def __init__(self, message: str, *, status_code: int | None = None, code: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


@dataclass(frozen=True, slots=True)
class TokenSet:
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    expires_in: int
    refresh_expires_in: int
    scopes: tuple[str, ...] | None

    @property
    def refresh_token_expires_in(self) -> int:
        """Legacy spelling retained for callers using the provider field name."""
        return self.refresh_expires_in


def _required_text(value: Any, name: str, *, max_len: int = 4096) -> str:
    if not isinstance(value, str):
        raise FeishuOAuthError(f"OAuth {name} 无效。")
    text = value.strip()
    if not text or len(text) > max_len or any(ord(c) < 0x20 or ord(c) == 0x7F for c in text):
        raise FeishuOAuthError(f"OAuth {name} 无效。")
    return text


def _token(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise FeishuOAuthError(f"飞书未返回有效 {name}。")
    token = value.strip()
    if not _TOKEN_RE.fullmatch(token):
        raise FeishuOAuthError(f"飞书未返回有效 {name}。")
    return token


def _redirect_uri(value: str) -> str:
    uri = _required_text(value, "redirect_uri", max_len=2048)
    parsed = urlsplit(uri)
    host = (parsed.hostname or "").lower()
    loopback = host in {"127.0.0.1", "localhost", "::1"}
    if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
        raise FeishuOAuthError("OAuth redirect_uri 必须使用 HTTPS；本机调试仅允许 loopback。")
    if not parsed.netloc or parsed.username or parsed.password or parsed.fragment or parsed.query:
        raise FeishuOAuthError("OAuth redirect_uri 格式不正确。")
    return uri


def _scopes(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        raw = value.split()
    elif isinstance(value, (list, tuple)):
        if not all(isinstance(item, str) for item in value):
            raise FeishuOAuthError("飞书未返回有效授权 scope。")
        raw = [item.strip() for item in value]
    else:
        raise FeishuOAuthError("飞书未返回实际授权 scope。")
    result = tuple(dict.fromkeys(item for item in raw if item))
    if not result or any(len(item) > 256 or any(ord(c) < 0x20 for c in item) for item in result):
        raise FeishuOAuthError("飞书未返回有效授权 scope。")
    return result


def _expiry(payload: Mapping[str, Any], key: str, *aliases: str) -> int:
    values = [payload[name] for name in (key, *aliases) if name in payload]
    if not values or len(set(values)) != 1:
        raise FeishuOAuthError(f"飞书未返回有效 {key}。")
    value = values[0]
    if type(value) is not int or value <= 0 or value > _MAX_EXPIRE_SECONDS:
        raise FeishuOAuthError(f"飞书未返回有效 {key}。")
    return value


class OAuthClient:
    """Bounded Feishu user OAuth client using the hardened API transport."""

    def __init__(self, *, endpoint: str | None = None,
                 transport: AsyncHttpTransport | None = None,
                 timeout_seconds: float = 20.0) -> None:
        if timeout_seconds <= 0 or timeout_seconds > 300:
            raise ValueError("timeout_seconds must be between 0 and 300")
        self.endpoint = _endpoint(endpoint, explicit=endpoint is not None)
        self.transport = transport or _Urllib3Transport()
        self.timeout_seconds = float(timeout_seconds)

    def authorization_url(self, app_id: str, redirect_uri: str, scopes: list[str] | tuple[str, ...],
                          state: str, challenge: str) -> str:
        client_id = _required_text(app_id, "app_id", max_len=256)
        redirect = _redirect_uri(redirect_uri)
        requested = _scopes(scopes)
        state_value = _required_text(state, "state", max_len=1024)
        challenge_value = _required_text(challenge, "challenge", max_len=128)
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", challenge_value):
            raise FeishuOAuthError("OAuth challenge 无效。")
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,512}", state_value):
            raise FeishuOAuthError("OAuth state 无效。")
        # Production authorization is hosted on the official accounts origin;
        # an explicit loopback endpoint remains available for deterministic tests.
        parsed = urlsplit(self.endpoint)
        origin = self.endpoint if (parsed.hostname or "").lower() in {"127.0.0.1", "localhost", "::1"} else "https://accounts.feishu.cn"
        return origin + AUTHORIZE_PATH + "?" + urlencode({
            "client_id": client_id, "redirect_uri": redirect,
            "scope": " ".join(requested), "state": state_value,
            "code_challenge": challenge_value, "code_challenge_method": "S256",
        })

    async def _request(self, method: str, path: str, body: Mapping[str, Any] | None = None,
                       *, access_token: str | None = None) -> dict[str, Any]:
        headers = {"Accept": "application/json", "Content-Type": "application/json; charset=utf-8"}
        if access_token:
            headers["Authorization"] = f"Bearer {_token(access_token, 'access_token')}"
        raw = json.dumps(dict(body), separators=(",", ":")).encode() if body is not None else None
        try:
            response = await asyncio.wait_for(self.transport.request(
                method, self.endpoint + path, headers=headers, body=raw,
                timeout_seconds=self.timeout_seconds), timeout=self.timeout_seconds * 2 + 1)
        except (asyncio.TimeoutError, FeishuApiError, OSError, ValueError) as exc:
            raise FeishuOAuthError("飞书 OAuth 请求失败。") from exc
        if not isinstance(response, HttpResponse) or len(response.body) > _MAX_RESPONSE_BYTES:
            raise FeishuOAuthError("飞书 OAuth 响应超过大小上限。")
        if response.status_code < 200 or response.status_code >= 300:
            raise FeishuOAuthError("飞书 OAuth 请求失败。", status_code=response.status_code)
        try:
            payload = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FeishuOAuthError("飞书 OAuth 返回格式不正确。", status_code=response.status_code) from exc
        if not isinstance(payload, dict) or payload.get("code") not in (None, 0):
            raise FeishuOAuthError("飞书 OAuth 请求失败。", status_code=response.status_code,
                                   code=payload.get("code") if isinstance(payload, dict) else None)
        return payload

    @staticmethod
    def _parse_tokens(payload: Mapping[str, Any], *, require_scopes: bool) -> TokenSet:
        raw_scopes = payload.get("scope", payload.get("scopes"))
        if raw_scopes is None and not require_scopes:
            scopes = None
        else:
            scopes = _scopes(raw_scopes)
        return TokenSet(
            access_token=_token(payload.get("access_token"), "access_token"),
            refresh_token=_token(payload.get("refresh_token"), "refresh_token"),
            expires_in=_expiry(payload, "expires_in"),
            refresh_expires_in=_expiry(payload, "refresh_expires_in", "refresh_token_expires_in"),
            scopes=scopes,
        )

    async def exchange(self, app_id: str, app_secret: str, code: str, redirect_uri: str, verifier: str) -> TokenSet:
        return self._parse_tokens(await self._request("POST", TOKEN_PATH, {
            "grant_type": "authorization_code", "client_id": _required_text(app_id, "app_id", max_len=256),
            "client_secret": _required_text(app_secret, "app_secret"), "code": _required_text(code, "code"),
            "redirect_uri": _redirect_uri(redirect_uri),
            "code_verifier": self._pkce_verifier(verifier),
        }), require_scopes=True)

    @staticmethod
    def _pkce_verifier(value: str) -> str:
        verifier = _required_text(value, "verifier", max_len=128)
        if not re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", verifier):
            raise FeishuOAuthError("OAuth verifier 无效。")
        return verifier

    async def refresh(self, app_id: str, app_secret: str, refresh_token: str) -> TokenSet:
        return self._parse_tokens(await self._request("POST", TOKEN_PATH, {
            "grant_type": "refresh_token", "client_id": _required_text(app_id, "app_id", max_len=256),
            "client_secret": _required_text(app_secret, "app_secret"),
            "refresh_token": _token(refresh_token, "refresh_token"),
        }), require_scopes=False)

    async def userinfo(self, access_token: str) -> dict[str, Any]:
        payload = await self._request("GET", USERINFO_PATH, access_token=access_token)
        data = payload.get("data")
        if not isinstance(data, dict):
            raise FeishuOAuthError("飞书 userinfo 返回格式不正确。")
        return dict(data)


__all__ = ["FeishuOAuthError", "OAuthClient", "TokenSet"]
