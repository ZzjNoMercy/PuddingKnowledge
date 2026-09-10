import pytest
from knowledge_platform.connector_sync.feishu_source import FeishuEntry, FeishuMedia, FeishuSource, FeishuSourceError

class Api:
    def __init__(self, blocks, files): self.blocks, self.files, self.calls = blocks, files, []
    async def get_docx_document(self, **kw): return {'revision_id': 1, 'title': 'doc'}
    async def list_docx_blocks(self, **kw): return self.blocks
    async def download_media_assets(self, **kw): self.calls.append(kw); return self.files

def entry(kind='docx', title='doc'): return FeishuEntry('id', None, 'tok', kind, title, (title,))

@pytest.mark.asyncio
async def test_materializes_duplicate_media_with_bounded_download():
    blocks=[{'block_id':'r','block_type':1,'children':['a','b']}, {'block_id':'a','block_type':23,'file':{'token':'a','name':'same.pdf'}}, {'block_id':'b','block_type':23,'file':{'token':'b','name':'same.pdf'}}]
    api=Api(blocks, {'a':(b'a','application/pdf',''), 'b':(b'b','application/pdf','')})
    doc=await FeishuSource(api).document(entry())
    assert isinstance(doc.media, tuple) and all(isinstance(x, FeishuMedia) for x in doc.media)
    assert len({x.relative_path for x in doc.media}) == 2
    assert api.calls[0]['max_bytes_each']==8*1024*1024 and api.calls[0]['max_total_bytes']==32*1024*1024
    assert all('url' not in x for x in doc.attachments)

@pytest.mark.asyncio
async def test_missing_media_and_count_fail_closed():
    api=Api([{'block_id':'a','block_type':27,'image':{'token':'x'}}], {})
    with pytest.raises(FeishuSourceError, match='incomplete'): await FeishuSource(api).document(entry())
    blocks=[{'block_id':str(i),'block_type':27,'image':{'token':str(i)}} for i in range(129)]
    api=Api(blocks, {})
    with pytest.raises(FeishuSourceError, match='count'): await FeishuSource(api).document(entry())
    assert api.calls == []

@pytest.mark.asyncio
async def test_drive_text_pdf_and_extension():
    class Drive:
        async def download_drive_file(self, **kw): return b'# hi', 'text/plain', ''
    doc=await FeishuSource(Drive()).drive_document(entry('file','x.md'))
    assert doc.markdown == b'# hi'
    class Pdf(Drive):
        async def download_drive_file(self, **kw): return b'%PDF', 'application/pdf', ''
    pdf=await FeishuSource(Pdf()).drive_document(entry('file','x.pdf'))
    assert pdf.raw == b'%PDF' and pdf.markdown == b''
    with pytest.raises(FeishuSourceError, match='supported'): await FeishuSource(Drive()).drive_document(entry('file','x.docx'))
