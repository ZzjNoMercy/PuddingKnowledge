import asyncio
import json

import pytest

from knowledge_platform.connector_sync.feishu_api import FeishuApiError, HttpResponse
from knowledge_platform.connector_sync.feishu_auth import (
    FeishuAppConfig,
    FeishuAuthError,
    ReauthenticatingFeishuApi,
    TenantTokenBroker,
)


class Provider:
    def __init__(self, value):
        self.value = value
        self.calls = 0

    def get(self, reference):
        assert reference.startswith("vault://users/test/credentials/")
        self.calls += 1
        return self.value


class Transport:
    def __init__(self, *, delay=0):
        self.calls = []
        self.delay = delay

    async def request(self, method, url, *, headers, body, timeout_seconds):
        self.calls.append((method, url, headers, json.loads(body)))
        if self.delay:
            await asyncio.sleep(self.delay)
        return HttpResponse(200, {"content-type": "application/json"}, json.dumps({
            "code": 0, "tenant_access_token": "tenant-token", "expire": 3600,
        }).encode())


def test_tenant_token_is_singleflight_and_cached():
    async def run():
        provider = Provider(json.dumps({"app_id": "cli_app", "app_secret": "secret"}))
        transport = Transport(delay=0.01)
        broker = TenantTokenBroker(provider, transport=transport)
        app = FeishuAppConfig("cli_app", "vault://users/test/credentials/feishu")
        assert await asyncio.gather(*(broker.get(app) for _ in range(8))) == ["tenant-token"] * 8
        assert len(transport.calls) == 1
        method, url, headers, body = transport.calls[0]
        assert method == "POST"
        assert url.endswith("/open-apis/auth/v3/tenant_access_token/internal")
        assert "Authorization" not in headers
        assert body == {"app_id": "cli_app", "app_secret": "secret"}

    asyncio.run(run())


def test_loopback_endpoint_requires_explicit_configuration():
    provider = Provider(json.dumps({"app_id": "cli_app", "app_secret": "secret"}))
    with pytest.raises(ValueError):
        TenantTokenBroker(provider, endpoint="https://evil.example")
    TenantTokenBroker(provider, endpoint="http://127.0.0.1:9999", transport=Transport())


def test_errors_do_not_echo_secret_or_provider_body():
    class BadTransport(Transport):
        async def request(self, method, url, *, headers, body, timeout_seconds):
            return HttpResponse(500, {}, b'{"error":"super-secret-response"}')

    async def run():
        provider = Provider(json.dumps({"app_id": "cli_app", "app_secret": "super-secret"}))
        with pytest.raises(FeishuAuthError) as error:
            await TenantTokenBroker(provider, transport=BadTransport()).get(
                FeishuAppConfig("cli_app", "vault://users/test/credentials/feishu")
            )
        assert "super-secret" not in str(error.value)
        assert "provider" not in str(error.value)

    asyncio.run(run())


def test_cache_binding_and_vault_app_id_match():
    async def run():
        provider = Provider(json.dumps({"app_id": "cli_app", "app_secret": "secret"}))
        transport = Transport()
        broker = TenantTokenBroker(provider, transport=transport)
        await broker.get(FeishuAppConfig("cli_app", "vault://users/test/credentials/feishu"))
        await broker.get(FeishuAppConfig("cli_app", "vault://users/test/credentials/other"))
        assert len(transport.calls) == 2

        mismatched = Provider(json.dumps({"app_id": "cli_other", "app_secret": "secret"}))
        with pytest.raises(FeishuAuthError):
            await TenantTokenBroker(mismatched, transport=Transport()).get(
                FeishuAppConfig("cli_app", "vault://users/test/credentials/feishu")
            )

    asyncio.run(run())


def test_non_2xx_and_expire_bounds_are_rejected():
    class ResponseTransport(Transport):
        def __init__(self, status, payload):
            super().__init__()
            self.status = status
            self.payload = payload

        async def request(self, method, url, *, headers, body, timeout_seconds):
            return HttpResponse(self.status, {}, json.dumps(self.payload).encode())

    async def run():
        app = FeishuAppConfig("cli_app", "vault://users/test/credentials/feishu")
        provider = Provider(json.dumps({"app_id": "cli_app", "app_secret": "secret"}))
        for status, expire in ((302, 3600), (200, True), (200, 0), (200, 604801)):
            transport = ResponseTransport(status, {"code": 0, "tenant_access_token": "t", "expire": expire})
            with pytest.raises(FeishuAuthError):
                await TenantTokenBroker(provider, transport=transport).get(app)

    asyncio.run(run())


def test_api_wrapper_refreshes_once_for_invalid_token_only():
    async def run():
        provider = Provider(json.dumps({"app_id": "cli_app", "app_secret": "secret"}))
        auth_transport = Transport()
        broker = TenantTokenBroker(provider, transport=auth_transport)
        made = []

        class Client:
            def __init__(self, token):
                self.token = token

            async def list_spaces(self):
                made.append(self.token)
                if self.token == "tenant-token":
                    raise FeishuApiError("invalid", status_code=401)
                return ["ok"]

        # Force the second token response to differ from the first.
        class Auth(Transport):
            async def request(self, *args, **kwargs):
                self.calls.append((args, kwargs))
                value = "tenant-token" if len(self.calls) == 1 else "tenant-token-2"
                return HttpResponse(200, {}, json.dumps({"code": 0, "tenant_access_token": value, "expire": 3600}).encode())

        wrapper = ReauthenticatingFeishuApi(
            TenantTokenBroker(provider, transport=Auth()),
            FeishuAppConfig("cli_app", "vault://users/test/credentials/feishu"),
            lambda token: Client(token),
        )
        assert await wrapper.list_spaces() == ["ok"]
        assert made == ["tenant-token", "tenant-token-2"]

    asyncio.run(run())


def test_api_wrapper_does_not_retry_for_403_or_429_and_retries_once():
    async def run():
        provider = Provider(json.dumps({"app_id": "cli_app", "app_secret": "secret"}))
        app = FeishuAppConfig("cli_app", "vault://users/test/credentials/feishu")
        for error in (
            FeishuApiError("forbidden", status_code=403),
            FeishuApiError("rate limited", status_code=429),
        ):
            calls = []

            class Client:
                async def list_spaces(self):
                    calls.append(1)
                    raise error

            wrapper = ReauthenticatingFeishuApi(TenantTokenBroker(provider, transport=Transport()), app, lambda token: Client())
            with pytest.raises(FeishuApiError):
                await wrapper.list_spaces()
            assert calls == [1]

        class AlwaysInvalid:
            async def list_spaces(self):
                raise FeishuApiError("invalid", code=99991661)

        wrapper = ReauthenticatingFeishuApi(TenantTokenBroker(provider, transport=Transport()), app, lambda token: AlwaysInvalid())
        with pytest.raises(FeishuApiError) as error:
            await wrapper.list_spaces()
        assert error.value.code == 99991661

    asyncio.run(run())
