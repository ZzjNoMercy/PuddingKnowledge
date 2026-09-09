"""Small, independent Feishu tenant-token broker.

This module deliberately implements app authentication only.  It does not
implement OAuth, persist tokens, or discover credentials from ambient config.
The host supplies an opaque credential reference and a resolver for it.
"""

from __future__ import annotations

import asyncio
import json
import inspect
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Protocol
from urllib.parse import urlsplit

from .feishu_api import AsyncHttpTransport, FeishuApiError, HttpResponse


DEFAULT_ENDPOINT = "https://open.feishu.cn"
TENANT_TOKEN_PATH = "/open-apis/auth/v3/tenant_access_token/internal"
_TOKEN_RE = re.compile(r"^[A-Za-z0-9._~+/=-]{1,4096}$")
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_MAX_EXPIRE_SECONDS = 7 * 24 * 60 * 60


class FeishuAuthError(RuntimeError):
    """Safe authentication error which never includes provider response data."""


class CredentialProvider(Protocol):
    def get(self, reference: str) -> str | Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class FeishuAppConfig:
    """The only app configuration needed by :class:`TenantTokenBroker`.

    ``app_secret_ref`` is an opaque LocalCredentialStore-style reference.  The
    resolved value may be a JSON object containing ``app_id`` and
    ``app_secret``, or a plain app secret when ``app_id`` is supplied here.
    """

    app_id: str
    app_secret_ref: str
    endpoint: str = DEFAULT_ENDPOINT


@dataclass(frozen=True, slots=True)
class _CachedToken:
    value: str
    expires_at: datetime


def _endpoint(value: str, *, explicit: bool) -> str:
    parsed = urlsplit(str(value or "").strip().rstrip("/"))
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
        raise ValueError("Feishu endpoint must be an origin")
    host = (parsed.hostname or "").lower()
    if parsed.scheme == "https" and host == "open.feishu.cn" and parsed.port in (None, 443):
        return "https://open.feishu.cn"
    if explicit and parsed.scheme == "http" and host in {"127.0.0.1", "localhost", "::1"}:
        return f"http://{parsed.netloc}"
    raise ValueError("Feishu endpoint must be https://open.feishu.cn; HTTP is allowed only for explicit loopback tests")


def _config(value: FeishuAppConfig | Mapping[str, Any]) -> FeishuAppConfig:
    if isinstance(value, FeishuAppConfig):
        app = value
    elif isinstance(value, Mapping):
        app = FeishuAppConfig(
            app_id=str(value.get("app_id") or ""),
            app_secret_ref=str(value.get("app_secret_ref") or value.get("credential_ref") or ""),
            endpoint=str(value.get("endpoint") or value.get("api_base_url") or DEFAULT_ENDPOINT),
        )
    else:
        raise TypeError("app must be FeishuAppConfig or a mapping")
    if not app.app_id.strip() or len(app.app_id.strip()) > 256 or not app.app_secret_ref.strip():
        raise FeishuAuthError("飞书应用配置不完整。")
    return FeishuAppConfig(app.app_id.strip(), app.app_secret_ref.strip(), _endpoint(app.endpoint, explicit=app.endpoint != DEFAULT_ENDPOINT))


def _credentials(value: str | Mapping[str, Any], configured_app_id: str) -> tuple[str, str]:
    if isinstance(value, Mapping):
        payload = value
    else:
        try:
            decoded = json.loads(value)
        except (TypeError, json.JSONDecodeError) as exc:
            # A provider may return the secret alone; app_id remains config-owned.
            if not isinstance(value, str) or not value:
                raise FeishuAuthError("飞书应用凭据不可用。") from exc
            return configured_app_id, value
        if not isinstance(decoded, Mapping):
            raise FeishuAuthError("飞书应用凭据不可用。")
        payload = decoded
    app_id = str(payload.get("app_id") or "").strip()
    secret = str(payload.get("app_secret") or "")
    if app_id != configured_app_id or not secret or "\r" in secret or "\n" in secret:
        raise FeishuAuthError("飞书应用凭据不完整。")
    return app_id, secret


class TenantTokenBroker:
    """In-memory, per-app cached tenant token broker with async single-flight."""

    def __init__(
        self,
        credential_provider: CredentialProvider,
        *,
        transport: AsyncHttpTransport | None = None,
        endpoint: str | None = None,
        timeout_seconds: float = 20.0,
        refresh_skew_seconds: int = 60,
    ) -> None:
        if timeout_seconds <= 0 or timeout_seconds > 300:
            raise ValueError("timeout_seconds must be between 0 and 300")
        if refresh_skew_seconds < 0 or refresh_skew_seconds >= 86400:
            raise ValueError("refresh_skew_seconds is invalid")
        self.credential_provider = credential_provider
        self.transport = transport
        self.endpoint = _endpoint(endpoint or DEFAULT_ENDPOINT, explicit=endpoint is not None)
        self.timeout_seconds = float(timeout_seconds)
        self.refresh_skew_seconds = refresh_skew_seconds
        self._cache: dict[tuple[str, str, str], _CachedToken] = {}
        self._locks: dict[tuple[str, str, str], asyncio.Lock] = {}

    def invalidate(self, app: FeishuAppConfig | Mapping[str, Any] | str) -> None:
        if isinstance(app, str):
            key = next((key for key in self._cache if key[0] == app), None)
            if key is None:
                return
        else:
            config = _config(app)
            effective_endpoint = config.endpoint if config.endpoint != DEFAULT_ENDPOINT else self.endpoint
            key = (config.app_id, config.app_secret_ref, effective_endpoint)
        self._cache.pop(key, None)

    async def get(self, app: FeishuAppConfig | Mapping[str, Any], *, force_refresh: bool = False) -> str:
        config = _config(app)
        effective_endpoint = config.endpoint if config.endpoint != DEFAULT_ENDPOINT else self.endpoint
        key = (config.app_id, config.app_secret_ref, effective_endpoint)
        now = datetime.now(timezone.utc)
        cached = self._cache.get(key)
        if not force_refresh and cached and cached.expires_at > now + timedelta(seconds=self.refresh_skew_seconds):
            return cached.value
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            now = datetime.now(timezone.utc)
            cached = self._cache.get(key)
            if not force_refresh and cached and cached.expires_at > now + timedelta(seconds=self.refresh_skew_seconds):
                return cached.value
            try:
                raw = self.credential_provider.get(config.app_secret_ref)
                app_id, app_secret = _credentials(raw, config.app_id)
                body = json.dumps({"app_id": app_id, "app_secret": app_secret}, separators=(",", ":")).encode()
                transport = self.transport
                if transport is None:
                    from .feishu_api import _Urllib3Transport
                    transport = _Urllib3Transport()
                response = await asyncio.wait_for(transport.request(
                    "POST", effective_endpoint + TENANT_TOKEN_PATH,
                    headers={"Accept": "application/json", "Content-Type": "application/json; charset=utf-8"},
                    body=body, timeout_seconds=self.timeout_seconds,
                ), timeout=self.timeout_seconds * 2 + 1)
                if len(response.body) > _MAX_RESPONSE_BYTES:
                    raise FeishuAuthError("飞书 tenant token 响应超过大小上限。")
                payload = json.loads(response.body.decode("utf-8"))
            except FeishuAuthError:
                raise
            except (asyncio.TimeoutError, FeishuApiError, OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
                raise FeishuAuthError("飞书 tenant token 请求失败。") from exc
            if not isinstance(payload, Mapping) or not 200 <= response.status_code < 300 or payload.get("code") not in (None, 0):
                raise FeishuAuthError("飞书 tenant token 请求失败。")
            token = str(payload.get("tenant_access_token") or "").strip()
            if not _TOKEN_RE.fullmatch(token):
                raise FeishuAuthError("飞书未返回有效 tenant token。")
            expires_value = payload.get("expire")
            if type(expires_value) is not int:
                raise FeishuAuthError("飞书未返回有效 tenant token 有效期。")
            expires_in = expires_value
            if expires_in <= 0 or expires_in > _MAX_EXPIRE_SECONDS:
                raise FeishuAuthError("飞书未返回有效 tenant token 有效期。")
            self._cache[key] = _CachedToken(token, now + timedelta(seconds=expires_in))
            return token


class ReauthenticatingFeishuApi:
    """Lazy API facade which retries one invalid tenant token exactly once.

    ``client_factory`` receives a token and returns a configured
    ``FeishuApiClient`` (or compatible client).  Construction does not fetch a
    token; the first token request happens when a client method is called.
    """

    _INVALID_TOKEN_CODES = {99991661, 99991663, 99991664, 99991668}

    def __init__(self, broker: TenantTokenBroker, app: FeishuAppConfig | Mapping[str, Any], client_factory) -> None:
        self.broker = broker
        self.app = _config(app)
        if not callable(client_factory):
            raise TypeError("client_factory must be callable")
        self.client_factory = client_factory

    @classmethod
    def _is_invalid_token(cls, error: BaseException) -> bool:
        return isinstance(error, FeishuApiError) and (
            error.status_code == 401 or error.code in cls._INVALID_TOKEN_CODES
        )

    async def _client(self, *, force_refresh: bool = False):
        token = await self.broker.get(self.app, force_refresh=force_refresh)
        client = self.client_factory(token)
        if inspect.isawaitable(client):
            client = await client
        return client

    def __getattr__(self, name: str):
        async def invoke(*args, **kwargs):
            client = await self._client()
            try:
                method = getattr(client, name)
                result = method(*args, **kwargs)
                return await result if inspect.isawaitable(result) else result
            except FeishuApiError as first_error:
                if not self._is_invalid_token(first_error):
                    raise
            client = await self._client(force_refresh=True)
            method = getattr(client, name)
            result = method(*args, **kwargs)
            return await result if inspect.isawaitable(result) else result

        return invoke


__all__ = [
    "CredentialProvider", "FeishuAppConfig", "FeishuAuthError", "TenantTokenBroker",
    "ReauthenticatingFeishuApi",
]
