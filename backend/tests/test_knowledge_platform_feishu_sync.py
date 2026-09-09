from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from knowledge_platform.catalog.metadata import KNOWLEDGE_METADATA
from knowledge_platform.catalog.models import (
    KnowledgeConnector,
    KnowledgeSourceItem,
    KnowledgeSyncRun,
)
from knowledge_platform.connector_sync.feishu_source import FeishuSource
from knowledge_platform.local.feishu_sync import FeishuSyncError, FeishuSyncService


class FakeFeishuApi:
    def __init__(self) -> None:
        self.entries = True
        self.revision = 7
        self.document_calls = 0
        self.block_calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.wait_in_document = False
        self.fail_discovery = False
        self.fail_document_once = False
        self.on_document = None

    async def list_nodes(self, *, space_id: str, parent_node_token: str | None = None):
        if self.fail_discovery:
            raise RuntimeError("remote discovery failed")
        if not self.entries:
            return []
        return [{
            "node_token": "node_1",
            "obj_token": "doc_1",
            "obj_type": "docx",
            "title": "One",
            "has_child": False,
        }]

    async def get_docx_document(self, *, document_id: str):
        self.document_calls += 1
        if self.fail_document_once:
            self.fail_document_once = False
            raise RuntimeError("document failed")
        if self.wait_in_document:
            self.started.set()
            await self.release.wait()
        if self.on_document is not None:
            self.on_document()
        return {"revision_id": self.revision, "title": "One"}

    async def list_docx_blocks(self, *, document_id: str, document_revision_id: int):
        self.block_calls += 1
        return [{
            "block_id": "block_1",
            "block_type": 2,
            "text": {"elements": [{"text_run": {"content": "hello"}}]},
        }]


def _fixture(tmp_path: Path):
    catalog = tmp_path / "catalog.db"
    engine = create_engine(f"sqlite:///{catalog}")
    KNOWLEDGE_METADATA.create_all(engine)
    with engine.begin() as connection:
        connection.execute(KnowledgeConnector.__table__.insert().values(
            id="connector_1", space_id="space_1", connector_key="feishu",
            name="Feishu", status="ready", auth_type="builtin", credential_ref="cred_1",
            config_json={"selection": {"kind": "wiki", "root": "", "wiki_space": "space_token"}},
            schedule_json={}, last_error_json={},
        ))
    return catalog, engine


def _run(engine, run_id: str):
    with Session(engine) as session:
        return session.scalar(select(KnowledgeSyncRun).where(KnowledgeSyncRun.id == run_id))


def _item(engine):
    with Session(engine) as session:
        return session.scalar(select(KnowledgeSourceItem))


async def _sync(service, api, *, key="k", mode="incremental"):
    return await service.sync(
        connector_id="connector_1", space_id="space_1", idempotency_key=key,
        source=FeishuSource(api), mode=mode,
    )


def test_revision_fast_path_and_same_key_retry(tmp_path: Path):
    catalog, engine = _fixture(tmp_path)
    service = FeishuSyncService(catalog, tmp_path / "state")
    api = FakeFeishuApi()
    try:
        first = asyncio.run(_sync(service, api, key="same"))
        assert first["changed"] == 1
        assert api.document_calls == 1
        item = _item(engine)
        assert item is not None and item.content_digest
        assert service.objects.read(item.content_digest) == b"hello\n"
        second = asyncio.run(_sync(service, api, key="same"))
        assert second["run_id"] == first["run_id"]
        assert second["changed"] == 1

        api.fail_document_once = True
        with pytest.raises(RuntimeError):
            asyncio.run(_sync(service, api, key="retry"))
        retried = asyncio.run(_sync(service, api, key="retry"))
        assert retried["unchanged"] == 1
        run = _run(engine, retried["run_id"])
        assert run is not None and run.attempt == 2
    finally:
        service.close()
        engine.dispose()


def test_full_scan_does_not_delete_after_discovery_failure(tmp_path: Path):
    catalog, engine = _fixture(tmp_path)
    service = FeishuSyncService(catalog, tmp_path / "state")
    api = FakeFeishuApi()
    try:
        asyncio.run(_sync(service, api, key="full-1", mode="full"))
        api.fail_discovery = True
        with pytest.raises(RuntimeError):
            asyncio.run(_sync(service, api, key="full-2", mode="full"))
        item = _item(engine)
        assert item is not None and item.status == "ready"
    finally:
        service.close()
        engine.dispose()


def test_full_scan_deletes_an_item_after_complete_empty_discovery(tmp_path: Path):
    catalog, engine = _fixture(tmp_path)
    service = FeishuSyncService(catalog, tmp_path / "state")
    api = FakeFeishuApi()
    try:
        asyncio.run(_sync(service, api, key="delete-1", mode="full"))
        api.entries = False
        result = asyncio.run(_sync(service, api, key="delete-2", mode="full"))
        assert result["deleted"] == 1
        item = _item(engine)
        assert item is not None and item.status == "deleted"
    finally:
        service.close()
        engine.dispose()


def test_incremental_empty_discovery_does_not_delete(tmp_path: Path):
    catalog, engine = _fixture(tmp_path)
    service = FeishuSyncService(catalog, tmp_path / "state")
    api = FakeFeishuApi()
    try:
        asyncio.run(_sync(service, api, key="incremental-1"))
        api.entries = False
        result = asyncio.run(_sync(service, api, key="incremental-2"))
        assert result["deleted"] == 0
        item = _item(engine)
        assert item is not None and item.status == "ready"
    finally:
        service.close()
        engine.dispose()


def test_selection_change_is_rejected_on_next_run(tmp_path: Path):
    catalog, engine = _fixture(tmp_path)
    service = FeishuSyncService(catalog, tmp_path / "state")
    api = FakeFeishuApi()
    try:
        asyncio.run(_sync(service, api, key="selection-1"))
        with engine.begin() as connection:
            connection.execute(KnowledgeConnector.__table__.update().values(
                config_json={"selection": {"kind": "wiki", "root": "root_2", "wiki_space": "space_token"}},
            ))
        with pytest.raises(FeishuSyncError, match="Selection change"):
            asyncio.run(_sync(service, api, key="selection-2"))
    finally:
        service.close()
        engine.dispose()


def test_same_idempotency_key_cannot_change_mode(tmp_path: Path):
    catalog, engine = _fixture(tmp_path)
    service = FeishuSyncService(catalog, tmp_path / "state")
    api = FakeFeishuApi()
    try:
        asyncio.run(_sync(service, api, key="mode", mode="incremental"))
        with pytest.raises(FeishuSyncError, match="idempotency key"):
            asyncio.run(_sync(service, api, key="mode", mode="full"))
    finally:
        service.close()
        engine.dispose()


def test_missing_object_rejects_revision_fast_path(tmp_path: Path):
    catalog, engine = _fixture(tmp_path)
    service = FeishuSyncService(catalog, tmp_path / "state")
    api = FakeFeishuApi()
    try:
        asyncio.run(_sync(service, api, key="missing-1"))
        item = _item(engine)
        assert item is not None and item.content_digest
        (service.objects.root / item.content_digest.removeprefix("sha256:")).unlink()
        with pytest.raises((FeishuSyncError, FileNotFoundError, ValueError)):
            asyncio.run(_sync(service, api, key="missing-2"))
    finally:
        service.close()
        engine.dispose()


def test_cancel_marks_run_failed_and_does_not_delete(tmp_path: Path):
    catalog, engine = _fixture(tmp_path)
    service = FeishuSyncService(catalog, tmp_path / "state")
    api = FakeFeishuApi()
    api.wait_in_document = True

    async def scenario():
        task = asyncio.create_task(_sync(service, api, key="cancel"))
        await api.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    try:
        asyncio.run(scenario())
        run = _run(engine, "feishu_sync_" + __import__("hashlib").sha256(b"space_1\x00cancel").hexdigest()[:48])
        assert run is not None and run.status == "failed"
        assert _item(engine) is None
    finally:
        service.close()
        engine.dispose()


def test_replaced_lease_owner_blocks_write(tmp_path: Path):
    catalog, engine = _fixture(tmp_path)
    service = FeishuSyncService(catalog, tmp_path / "state")
    api = FakeFeishuApi()

    def replace_owner():
        with engine.begin() as connection:
            connection.execute(KnowledgeSyncRun.__table__.update().values(lease_owner="other-owner"))

    api.on_document = replace_owner
    try:
        with pytest.raises(FeishuSyncError, match="ownership"):
            asyncio.run(_sync(service, api, key="owner"))
        assert _item(engine) is None
    finally:
        service.close()
        engine.dispose()


def test_configuration_change_blocks_write(tmp_path: Path):
    catalog, engine = _fixture(tmp_path)
    service = FeishuSyncService(catalog, tmp_path / "state")
    api = FakeFeishuApi()

    def change_config():
        with engine.begin() as connection:
            connection.execute(KnowledgeConnector.__table__.update().values(
                config_json={"selection": {"kind": "wiki", "root": "root_2", "wiki_space": "space_token"}},
            ))

    api.on_document = change_config
    try:
        with pytest.raises(FeishuSyncError, match="configuration"):
            asyncio.run(_sync(service, api, key="config"))
        assert _item(engine) is None
    finally:
        service.close()
        engine.dispose()
