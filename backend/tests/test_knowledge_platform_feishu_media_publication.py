from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from knowledge_platform.catalog.metadata import KNOWLEDGE_METADATA
from knowledge_platform.catalog.models import KnowledgeAsset, KnowledgeConnector, KnowledgeSourceItem
from knowledge_platform.connector_sync.feishu_source import FeishuDocument, FeishuEntry
from knowledge_platform.local.feishu_media import stage_document
from knowledge_platform.local.feishu_sync import FeishuSyncService
from knowledge_platform.parsers.contracts import ParsedDocument, ParsedMedia


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class Media:
    token: str
    block_id: str
    filename: str
    content: bytes
    mime: str
    relative_path: str

    @property
    def mime_type(self) -> str:
        return self.mime


def _document(revision: str, *, media=(), markdown: bytes = b"# doc\n"):
    document = FeishuDocument(revision, "One", b"raw-" + revision.encode(), markdown, (), ())
    # The source contract is being extended with ``media``; keep this test
    # runnable against the current checkout while exercising that contract.
    object.__setattr__(document, "media", tuple(media))
    return document


class Source:
    def __init__(self, documents, *, fail=False):
        self.documents = documents
        self.fail = fail
        self.entry = FeishuEntry("wiki:space:doc_1", None, "doc_1", "docx", "One", ("One",))

    async def discover(self, selection):
        return (self.entry,)

    async def document(self, entry, *, previous_revision=None):
        if self.fail:
            raise RuntimeError("download failed")
        document = self.documents[-1]
        if previous_revision == document.revision:
            return None
        return document


def _fixture(tmp_path: Path, parser=None):
    catalog = tmp_path / "catalog.db"
    engine = create_engine(f"sqlite:///{catalog}")
    KNOWLEDGE_METADATA.create_all(engine)
    config = {"selection": {"kind": "wiki", "root": "", "wiki_space": "space"}}
    if parser is not None:
        config["parser"] = parser
    with engine.begin() as connection:
        connection.execute(KnowledgeConnector.__table__.insert().values(
            id="connector_1", space_id="space_1", connector_key="feishu", name="Feishu",
            status="ready", auth_type="builtin", credential_ref="cred_1", config_json=config,
            schedule_json={}, last_error_json={},
        ))
    return catalog, engine


def _item(engine):
    with Session(engine) as session:
        return session.scalar(select(KnowledgeSourceItem))


@pytest.mark.asyncio
async def test_stage_media_creates_real_original_and_parser_derivatives(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    class Parser:
        def __init__(self, *args, **kwargs): pass

        async def parse_pdf(self, content, filename):
            return ParsedDocument(
                markdown=b"# parsed\n![figure](images/figure.png)\n",
                assets=(ParsedMedia("images/figure.png", b"PNG", "image/png"),),
                parser_id="mineru", version="test-v1",
            )

    monkeypatch.setattr("knowledge_platform.parsers.mineru.MinerUClient", Parser)
    objects = SimpleNamespace(put=lambda data: _digest(data))
    media = Media("file-1", "block-1", "report.pdf", b"%PDF original", "application/pdf", "assets/report.pdf")
    document, staged = await stage_document(
        objects, space="space_1", item="item_1", document=_document("1", media=(media,), markdown=b"# doc\n[report](assets/report.pdf)\n"),
        parser_config={"id": "mineru_local", "endpoint": "http://127.0.0.1:8002", "timeout": 30},
    )
    originals = [item for item in staged if item.kind == "attachment"]
    derivatives = [item for item in staged if item.kind == "parsed_document"]
    assert len(originals) == len(derivatives) == 1
    assert originals[0].digest != derivatives[0].digest
    assert derivatives[0].metadata["original_asset_id"] == originals[0].id
    assert derivatives[0].metadata["parser_version"] == "test-v1"
    assert originals[0].uri in document.markdown.decode()


def test_media_failure_does_not_update_existing_publication(tmp_path: Path):
    catalog, engine = _fixture(tmp_path)
    service = FeishuSyncService(catalog, tmp_path / "state")
    first = Source([_document("1")])
    try:
        asyncio.run(service.sync(connector_id="connector_1", space_id="space_1", idempotency_key="first", source=first))
        before = _item(engine)
        assert before is not None and before.status == "ready"
        with pytest.raises(RuntimeError):
            asyncio.run(service.sync(connector_id="connector_1", space_id="space_1", idempotency_key="failed", source=Source([_document("2")], fail=True)))
        after = _item(engine)
        assert after is not None and after.revision == before.revision and after.content_digest == before.content_digest
    finally:
        service.close(); engine.dispose()


def test_full_deletion_revokes_media_source_binding(tmp_path: Path):
    catalog, engine = _fixture(tmp_path)
    service = FeishuSyncService(catalog, tmp_path / "state")
    source = Source([_document("1")])
    try:
        asyncio.run(service.sync(connector_id="connector_1", space_id="space_1", idempotency_key="first", source=source))
        class Empty:
            async def discover(self, selection): return ()
        result = asyncio.run(service.sync(connector_id="connector_1", space_id="space_1", idempotency_key="delete", mode="full", source=Empty()))
        assert result["deleted"] == 1
        assert _item(engine).status == "deleted"
    finally:
        service.close(); engine.dispose()


def test_parser_config_change_forces_new_revision_fetch(tmp_path: Path):
    parser_a = {"id": "mineru_local", "endpoint": "http://127.0.0.1:8002", "timeout": 30}
    catalog, engine = _fixture(tmp_path, parser=parser_a)
    service = FeishuSyncService(catalog, tmp_path / "state")
    try:
        asyncio.run(service.sync(connector_id="connector_1", space_id="space_1", idempotency_key="first", source=Source([_document("1", markdown=b"# a\n")])) )
        first = _item(engine)
        with engine.begin() as connection:
            connection.execute(KnowledgeConnector.__table__.update().where(KnowledgeConnector.id == "connector_1").values(
                config_json={"selection": {"kind": "wiki", "root": "", "wiki_space": "space"}, "parser": {**parser_a, "timeout": 31}},
            ))
        asyncio.run(service.sync(connector_id="connector_1", space_id="space_1", idempotency_key="second", source=Source([_document("1", markdown=b"# reparsed\n")])) )
        second = _item(engine)
        assert first is not None and second is not None
        assert second.content_digest != first.content_digest
    finally:
        service.close(); engine.dispose()


@pytest.mark.asyncio
async def test_same_parser_config_with_changed_output_has_new_publication_identity(monkeypatch):
    class Parser:
        body=b'first result'
        def __init__(self,*args,**kwargs):pass
        async def parse_pdf(self,*args):return ParsedDocument(self.body,(),'mineru','v1')
    monkeypatch.setattr('knowledge_platform.parsers.mineru.MinerUClient',Parser)
    objects=SimpleNamespace(put=_digest)
    document=_document('1',media=(Media('t','b','a.pdf',b'%PDF same','application/pdf','assets/a.pdf'),))
    _,first=await stage_document(objects,space='space_1',item='item',document=document,parser_config={'endpoint':'http://127.0.0.1:8000'})
    Parser.body=b'second result'
    _,second=await stage_document(objects,space='space_1',item='item',document=document,parser_config={'endpoint':'http://127.0.0.1:8000'})
    a=next(x for x in first if x.kind=='attachment');b=next(x for x in second if x.kind=='attachment')
    assert a.digest==b.digest and a.id!=b.id
    assert a.metadata['derivatives']!=b.metadata['derivatives']
