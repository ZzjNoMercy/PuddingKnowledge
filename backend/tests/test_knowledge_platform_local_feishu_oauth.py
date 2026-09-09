import asyncio
import json
import threading
from datetime import datetime, timezone
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


class _LocalOAuthClient:
    def __init__(self, *, exchange_hook=None, exchange_gate=None, refresh_gate=None, refresh_error=None, exchange_scopes=("scope-a",)):
        self.exchange_hook = exchange_hook
        self.exchange_gate = exchange_gate
        self.refresh_gate = refresh_gate
        self.refresh_error = refresh_error
        self.exchange_scopes = exchange_scopes
        self.authorization_calls = 0
        self.exchange_calls = 0
        self.refresh_calls = 0
        self._refresh_no = 0

    def authorization_url(self, *, app_id, redirect_uri, scopes, state, challenge):
        self.authorization_calls += 1
        return f"https://accounts.feishu.cn/open-apis/authen/v1/authorize?state={state}"

    async def exchange(self, **kwargs):
        self.exchange_calls += 1
        if self.exchange_hook:
            self.exchange_hook()
        if self.exchange_gate:
            await self.exchange_gate.wait()
        return TokenSet("access-1", "refresh-1", 3600, 86400, self.exchange_scopes)

    async def userinfo(self, *, access_token):
        return {"open_id": "ou-user-1", "union_id": "union-1", "tenant_key": "tenant-1"}

    async def refresh(self, **kwargs):
        self.refresh_calls += 1
        if self.refresh_error:
            raise self.refresh_error
        if self.refresh_gate:
            await self.refresh_gate.wait()
        self._refresh_no += 1
        n = self._refresh_no + 1
        return TokenSet(f"access-{n}", f"refresh-{n}", 3600, 86400, ("scope-a",))


def _local_oauth_fixture(tmp_path: Path, client):
    engine = create_engine(f"sqlite:///{tmp_path / 'catalog.db'}")
    KNOWLEDGE_METADATA.create_all(engine)
    vault = LocalCredentialStore(tmp_path / "state", owner_user_id="feishu")
    app_ref = vault.put("app", json.dumps({"app_id": "cli_test", "app_secret": "app-secret"}))
    connector = KnowledgeConnector(
        id="source-1", space_id="space-1", connector_key="feishu", name="Feishu", status="ready", auth_type="user",
        credential_ref=app_ref,
        config_json={"credential_id": "credential-1", "app_id": "cli_test", "endpoint": "http://127.0.0.1:9911",
                     "oauth_redirect_uris": ["http://127.0.0.1/callback"], "oauth_scopes": ["scope-a"],
                     "oauth_principal": "principal-1", "authorization_generation": 0},
    )
    with Session(engine) as session, session.begin():
        session.add(connector)
    service = LocalFeishuOAuth(engine, vault, tmp_path / "state", client_factory=lambda endpoint: client)
    return engine, vault, service


async def _local_authorize(service, client):
    started = await service.start("source-1", principal_id="principal-1", redirect_uri="http://127.0.0.1/callback")
    state = parse_qs(urlsplit(started["authorization_url"]).query)["state"][0]
    return started, await service.complete(state=state, code="code-1", principal_id="principal-1")


def test_local_oauth_state_is_one_shot_before_network_and_principal_bound(tmp_path):
    seen_status = []
    client = _LocalOAuthClient()
    engine, _, service = _local_oauth_fixture(tmp_path, client)
    def observe_exchange_state():
        with Session(engine) as session:
            seen_status.append(session.scalar(select(KnowledgeOAuthSession)).status)
    client.exchange_hook = observe_exchange_state

    async def run():
        started = await service.start("source-1", principal_id="principal-1", redirect_uri="http://127.0.0.1/callback")
        state = parse_qs(urlsplit(started["authorization_url"]).query)["state"][0]
        with pytest.raises(OAuthStateError, match="unavailable"):
            await service.complete(state=state, code="code-1", principal_id="principal-2")
        assert client.exchange_calls == 0
        await service.complete(state=state, code="code-1", principal_id="principal-1")
        assert client.exchange_calls == 1
        with pytest.raises(OAuthStateError, match="already used"):
            await service.complete(state=state, code="code-1", principal_id="principal-1")
        assert seen_status == ["exchanging"]
    asyncio.run(run())


def test_local_oauth_concurrent_refresh_rotates_once(tmp_path):
    client = _LocalOAuthClient()
    engine, _, service = _local_oauth_fixture(tmp_path, client)

    async def run():
        await _local_authorize(service, client)
        with Session(engine) as session, session.begin():
            grant = session.query(KnowledgeCredentialGrant).one()
            grant.access_expires_at = grant.access_expires_at.replace(year=2000)
            binding = _binding(session.get(KnowledgeConnector, "source-1"))
        result = await asyncio.gather(*(
            service.get_token("source-1", principal_id="principal-1", expected_binding=binding),
            service.get_token("source-1", principal_id="principal-1", expected_binding=binding),
        ))
        assert result == ["access-2", "access-2"]
        assert client.refresh_calls == 1
        with Session(engine) as session:
            assert session.query(KnowledgeCredentialGrant).one().token_version == 2
    asyncio.run(run())


def test_local_oauth_revoke_preempts_refresh_and_failure_does_not_resurrect(tmp_path):
    gate = asyncio.Event()
    client = _LocalOAuthClient(refresh_gate=gate)
    engine, _, service = _local_oauth_fixture(tmp_path, client)

    async def run():
        await _local_authorize(service, client)
        with Session(engine) as session, session.begin():
            grant = session.query(KnowledgeCredentialGrant).one()
            grant.access_expires_at = grant.access_expires_at.replace(year=2000)
            binding = _binding(session.get(KnowledgeConnector, "source-1"))
        task = asyncio.create_task(service.get_token("source-1", principal_id="principal-1", expected_binding=binding))
        while client.refresh_calls == 0:
            await asyncio.sleep(0)
        assert service.revoke("source-1", principal_id="principal-1")["provider_revocation"] is False
        gate.set()
        with pytest.raises(OAuthStateError):
            await task
        with Session(engine) as session:
            grant = session.query(KnowledgeCredentialGrant).one()
            assert grant.status == "revoked" and grant.token_version == 2
        # A revoked grant must never be revived by a later token request.
        with pytest.raises(OAuthStateError):
            await service.get_token("source-1", principal_id="principal-1", expected_binding=binding)
    asyncio.run(run())


def test_local_oauth_refresh_exception_and_restart_refreshing_require_reauth(tmp_path):
    client = _LocalOAuthClient()
    engine, vault, service = _local_oauth_fixture(tmp_path, client)

    async def run():
        await _local_authorize(service, client)
        with Session(engine) as session, session.begin():
            grant = session.query(KnowledgeCredentialGrant).one()
            grant.access_expires_at = grant.access_expires_at.replace(year=2000)
            binding = _binding(session.get(KnowledgeConnector, "source-1"))
        client.refresh_error = OAuthStateError("provider failed")
        with pytest.raises(OAuthStateError, match="provider failed"):
            await service.get_token("source-1", principal_id="principal-1", expected_binding=binding)
        with Session(engine) as session:
            assert session.query(KnowledgeCredentialGrant).one().status == "needs_reauth"
            session.query(KnowledgeCredentialGrant).one().status = "refreshing"
            session.get(KnowledgeConnector, "source-1").status = "ready"
            session.commit()
        restarted = LocalFeishuOAuth(engine, vault, tmp_path / "state", client_factory=lambda endpoint: client)
        with pytest.raises(OAuthStateError, match="reauthorization"):
            await restarted.get_token("source-1", principal_id="principal-1", expected_binding=binding)
        with Session(engine) as session:
            assert session.query(KnowledgeCredentialGrant).one().status == "needs_reauth"
    asyncio.run(run())


def test_local_oauth_expired_state_and_unallowlisted_redirect_do_not_call_provider(tmp_path):
    client = _LocalOAuthClient()
    engine, _, service = _local_oauth_fixture(tmp_path, client)

    async def run():
        with pytest.raises(OAuthStateError, match="not configured"):
            await service.start("source-1", principal_id="principal-1", redirect_uri="http://127.0.0.1/evil")
        assert client.authorization_calls == 0
        started = await service.start("source-1", principal_id="principal-1", redirect_uri="http://127.0.0.1/callback")
        state = parse_qs(urlsplit(started["authorization_url"]).query)["state"][0]
        with Session(engine) as session, session.begin():
            session.scalar(select(KnowledgeOAuthSession)).expires_at = datetime.now(timezone.utc).replace(year=2000)
        with pytest.raises(OAuthStateError, match="expired"):
            await service.complete(state=state, code="code-1", principal_id="principal-1")
        assert client.exchange_calls == 0
    asyncio.run(run())


def test_local_oauth_revoke_during_complete_prevents_callback_from_creating_grant(tmp_path):
    exchange_gate = asyncio.Event()
    exchange_started = asyncio.Event()
    client = _LocalOAuthClient(exchange_gate=exchange_gate)
    original_exchange = client.exchange

    async def exchange(**kwargs):
        exchange_started.set()
        return await original_exchange(**kwargs)
    client.exchange = exchange
    engine, _, service = _local_oauth_fixture(tmp_path, client)

    async def run():
        started = await service.start("source-1", principal_id="principal-1", redirect_uri="http://127.0.0.1/callback")
        state = parse_qs(urlsplit(started["authorization_url"]).query)["state"][0]
        callback = asyncio.create_task(service.complete(state=state, code="code-1", principal_id="principal-1"))
        await exchange_started.wait()
        service.revoke("source-1", principal_id="principal-1")
        exchange_gate.set()
        with pytest.raises(OAuthStateError):
            await callback
        with Session(engine) as session:
            assert session.query(KnowledgeCredentialGrant).count() == 0
            assert session.scalar(select(KnowledgeOAuthSession)).status == "revoked"
    asyncio.run(run())


def test_local_oauth_actual_scope_missing_is_rejected_without_requested_scope_fallback(tmp_path):
    client = _LocalOAuthClient(exchange_scopes=("other-scope",))
    engine, _, service = _local_oauth_fixture(tmp_path, client)

    async def run():
        started = await service.start("source-1", principal_id="principal-1", redirect_uri="http://127.0.0.1/callback")
        state = parse_qs(urlsplit(started["authorization_url"]).query)["state"][0]
        with pytest.raises(OAuthStateError, match="lacks"):
            await service.complete(state=state, code="code-1", principal_id="principal-1")
        with Session(engine) as session:
            assert session.query(KnowledgeCredentialGrant).count() == 0
    asyncio.run(run())


def test_local_oauth_force_refresh_same_rejected_token_is_singleflight(tmp_path):
    client = _LocalOAuthClient()
    engine, _, service = _local_oauth_fixture(tmp_path, client)

    async def run():
        await _local_authorize(service, client)
        with Session(engine) as session, session.begin():
            grant = session.query(KnowledgeCredentialGrant).one()
            binding = _binding(session.get(KnowledgeConnector, "source-1"))
            old_token = json.loads(service.vault.get(grant.token_credential_ref))["access_token"]
        result = await asyncio.gather(*(
            service.get_token("source-1", principal_id="principal-1", expected_binding=binding, force_refresh=True, rejected_token=old_token),
            service.get_token("source-1", principal_id="principal-1", expected_binding=binding, force_refresh=True, rejected_token=old_token),
        ))
        assert result == ["access-2", "access-2"]
        assert client.refresh_calls == 1
    asyncio.run(run())
