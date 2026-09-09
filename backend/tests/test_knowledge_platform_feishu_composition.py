from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from knowledge_contracts import Principal
from knowledge_platform.catalog.metadata import KNOWLEDGE_METADATA
from knowledge_platform.catalog.models import (
    KnowledgeAsset,
    KnowledgeConnector,
    KnowledgeSourceItem,
    KnowledgeSpace,
)
from knowledge_platform.connector_sync.feishu_source import FeishuSource
from knowledge_platform.local.feishu import LocalFeishuService
from knowledge_platform.local.feishu_sync import FeishuSyncError
from knowledge_platform.transport.fastapi_feishu_router import create_feishu_router


class _Api:
    def __init__(self) -> None:
        self.revision = 1
        self.entries = True

    async def list_nodes(self, *, space_id, parent_node_token=None):
        if not self.entries:
            return []
        return [{"node_token": "node_1", "obj_token": "doc_1", "obj_type": "docx", "title": "Doc"}]

    async def get_docx_document(self, *, document_id):
        return {"revision_id": self.revision, "title": "Doc"}

    async def list_docx_blocks(self, *, document_id, document_revision_id):
        return [{
            "block_id": "block_1", "block_type": 2,
            "text": {"elements": [{"text_run": {"content": "body"}}]},
        }]


def _fixture(tmp_path: Path):
    catalog = tmp_path / "catalog.db"
    engine = create_engine(f"sqlite:///{catalog}")
    KNOWLEDGE_METADATA.create_all(engine)
    with engine.begin() as connection:
        connection.execute(KnowledgeSpace.__table__.insert().values(
            id="space_kb_default", name="Default", description="", permissions_json={},
        ))
    config = {"version": 1, "sources": [{
        "id": "source_1", "name": "Feishu", "app_id": "app_1",
        "app_secret_env": "FEISHU_SECRET",
        "selection": {"kind": "wiki", "root": "", "wiki_space": "space_token"},
    }]}
    return catalog, engine, config


def _connector(engine):
    with Session(engine) as session:
        return session.get(KnowledgeConnector, "source_1")


def _item(engine):
    with Session(engine) as session:
        return session.scalar(select(KnowledgeSourceItem))


def _asset(engine, asset_id):
    with Session(engine) as session:
        return session.get(KnowledgeAsset, asset_id)


def _service(tmp_path, monkeypatch):
    catalog, engine, config = _fixture(tmp_path)
    monkeypatch.setenv("FEISHU_SECRET", "secret-1")
    service = LocalFeishuService(config, catalog, tmp_path / "state")
    return service, engine


def test_claim_binding_is_immutable_when_credential_changes_after_claim(tmp_path: Path, monkeypatch):
    service, engine = _service(tmp_path, monkeypatch)
    api = _Api()
    captured = []
    old_reference = _connector(engine).credential_ref

    async def factory(reference, binding):
        captured.append(reference)
        with engine.begin() as connection:
            connection.execute(KnowledgeConnector.__table__.update().where(
                KnowledgeConnector.id == "source_1"
            ).values(credential_ref="vault://new-ref"))
        return FeishuSource(api)

    service._bound_source = factory
    try:
        with pytest.raises(FeishuSyncError):
            asyncio.run(service.sync("source_1", idempotency_key="credential-race"))
        assert captured == [old_reference]
        assert _item(engine) is None
    finally:
        service.sync_service.close()
        engine.dispose()


def test_successful_same_key_replay_does_not_call_source_factory(tmp_path: Path, monkeypatch):
    service, engine = _service(tmp_path, monkeypatch)
    api = _Api()
    calls = 0

    async def factory(reference, binding):
        nonlocal calls
        calls += 1
        return FeishuSource(api)

    service._bound_source = factory
    try:
        first = asyncio.run(service.sync("source_1", idempotency_key="replay"))
        second = asyncio.run(service.sync("source_1", idempotency_key="replay"))
        assert first == second
        assert calls == 1
    finally:
        service.sync_service.close()
        engine.dispose()


def test_old_asset_and_selection_change_are_unreadable(tmp_path: Path, monkeypatch):
    service, engine = _service(tmp_path, monkeypatch)
    api = _Api()

    async def factory(reference, binding):
        return FeishuSource(api)

    service._bound_source = factory
    try:
        asyncio.run(service.sync("source_1", idempotency_key="asset-1"))
        old_item = _item(engine)
        assert old_item is not None and old_item.asset_id
        old_asset = _asset(engine, old_item.asset_id)
        assert old_asset is not None
        old_uri = old_asset.source_uri
        api.revision = 2
        asyncio.run(service.sync("source_1", idempotency_key="asset-2"))
        assert _item(engine).asset_id != old_asset.id
        with pytest.raises(LookupError, match="current source binding"):
            service.read_published(old_uri)

        with engine.begin() as connection:
            connection.execute(KnowledgeConnector.__table__.update().values(
                config_json={"selection": {"kind": "wiki", "root": "new_root", "wiki_space": "space_token"}, "app_id": "app_1", "endpoint": None, "credential_id": "x"},
            ))
        current_item = _item(engine)
        current_asset = _asset(engine, current_item.asset_id)
        with pytest.raises(LookupError, match="selection changed"):
            service.read_published(current_asset.source_uri)
    finally:
        service.sync_service.close()
        engine.dispose()


class _AdminService:
    def __init__(self):
        self.discover_calls = 0
        self.sync_calls = 0

    async def discover(self, source_id):
        self.discover_calls += 1
        return []

    async def sync(self, source_id, **body):
        self.sync_calls += 1
        return {"run_id": "run_1", "status": "succeeded"}


def test_admin_routes_require_admin_and_space_scope(tmp_path: Path):
    del tmp_path
    service = _AdminService()
    current = {"principal": Principal("p", scopes=("knowledge.admin",))}
    app = FastAPI()
    app.include_router(create_feishu_router(service, principal_provider=lambda: current["principal"]))
    with TestClient(app) as client:
        denied_discover = client.post("/v1/sources/source_1:discover")
        denied_sync = client.post("/v1/sources/source_1:sync", json={"idempotency_key": "x", "mode": "incremental"})
        assert denied_discover.json()["error"]["code"] == "permission_denied"
        assert denied_sync.json()["error"]["code"] == "permission_denied"
        current["principal"] = Principal("p", scopes=("knowledge.admin", "knowledge.space:space_kb_default"))
        assert client.post("/v1/sources/source_1:discover").json()["status"] == "ok"
        current["principal"] = Principal("p", scopes=("knowledge.space:space_kb_default",))
        assert client.post("/v1/sources/source_1:sync", json={"idempotency_key": "x", "mode": "incremental"}).json()["error"]["code"] == "permission_denied"
    assert service.discover_calls == 1
    assert service.sync_calls == 0
