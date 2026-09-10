import asyncio
from dataclasses import replace

import pytest
from sqlalchemy.orm import Session

from knowledge_platform.catalog.models import KnowledgeAsset
from knowledge_platform.connector_sync.feishu_source import FeishuDocument, FeishuEntry
from test_knowledge_platform_feishu_composition import _service, _item


class Source:
    body=b'first text'
    title='file.txt'
    async def discover(self, selection):
        return (FeishuEntry('drive:file',None,'token','file',self.title,(self.title,)),)
    async def drive_document(self, entry):
        return FeishuDocument('untrusted-token-revision',self.title,self.body,self.body,(),())


def test_drive_text_revision_is_content_and_derivative_survives_restart(tmp_path,monkeypatch):
    service,engine=_service(tmp_path,monkeypatch)
    source=Source()
    async def factory(*args):return source
    service._bound_source=factory
    try:
        asyncio.run(service.sync('source_1',idempotency_key='drive1',mode='full'))
        item=_item(engine)
        assert item.revision.startswith('sha256:')
        with Session(engine) as session:
            original=session.get(KnowledgeAsset,item.asset_id)
            assert original.kind=='original_file'
            target=service.derivative_targets(original.id)['normalized_markdown']
            derivative=session.get(KnowledgeAsset,target)
            old_uri=original.source_uri
            assert service.read_published(derivative.source_uri)==b'first text'
        result=asyncio.run(service.sync('source_1',idempotency_key='drive2'))
        assert result['unchanged']==1
        source.body=b'second text'
        asyncio.run(service.sync('source_1',idempotency_key='drive3'))
        assert _item(engine).asset_id!=original.id
        with pytest.raises(LookupError):service.read_published(old_uri)
    finally:service.sync_service.close();engine.dispose()


def test_drive_pdf_without_parser_retains_original_only(tmp_path,monkeypatch):
    service,engine=_service(tmp_path,monkeypatch)
    source=Source();source.title='file.pdf';source.body=b'%PDF-1.7 fixture'
    async def factory(*args):return source
    service._bound_source=factory
    try:
        asyncio.run(service.sync('source_1',idempotency_key='pdf'))
        item=_item(engine)
        assert item.metadata_json['parse_status']=='not_configured'
        assert service.derivative_targets(item.asset_id)=={}
        with Session(engine) as session:
            asset=session.get(KnowledgeAsset,item.asset_id)
            assert service.read_published(asset.source_uri)==source.body
    finally:service.sync_service.close();engine.dispose()


def test_media_parent_replacement_deletion_and_disable_revoke_reads(tmp_path,monkeypatch):
    from knowledge_platform.connector_sync.feishu_source import FeishuMedia
    from knowledge_platform.catalog.models import KnowledgeConnector
    service,engine=_service(tmp_path,monkeypatch)
    class MediaSource:
        revision='1';empty=False;fail=False
        async def discover(self,selection):
            return () if self.empty else (FeishuEntry('doc',None,'token','docx','Doc',('Doc',)),)
        async def document(self,entry,previous_revision=None):
            if self.fail:raise RuntimeError('download failed')
            return FeishuDocument(self.revision,'Doc',b'raw',b'![x](./assets/x.png)',(),(),
                (FeishuMedia('token','block','x.png',self.revision.encode(),'image/png','assets/x.png'),))
    source=MediaSource()
    async def factory(*args):return source
    service._bound_source=factory
    def uri():
        with Session(engine) as session:
            return session.get(KnowledgeAsset,_item(engine).metadata_json['published_media_ids'][0]).source_uri
    try:
        asyncio.run(service.sync('source_1',idempotency_key='a'))
        old=uri();assert service.read_published(old)==b'1'
        source.revision='2';source.fail=True
        with pytest.raises(RuntimeError):asyncio.run(service.sync('source_1',idempotency_key='b'))
        assert service.read_published(old)==b'1'
        source.fail=False
        asyncio.run(service.sync('source_1',idempotency_key='b'))
        current=uri();assert service.read_published(current)==b'2'
        with pytest.raises(LookupError):service.read_published(old)
        with Session(engine) as session,session.begin():session.get(KnowledgeConnector,'source_1').status='disabled'
        with pytest.raises(LookupError):service.read_published(current)
        with Session(engine) as session,session.begin():session.get(KnowledgeConnector,'source_1').status='ready'
        source.empty=True
        asyncio.run(service.sync('source_1',idempotency_key='deleted',mode='full'))
        with pytest.raises(LookupError):service.read_published(current)
    finally:service.sync_service.close();engine.dispose()
