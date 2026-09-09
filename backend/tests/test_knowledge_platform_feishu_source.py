import pytest
from knowledge_platform.connector_sync.feishu_source import FeishuSource, FeishuSelection, FeishuSourceError

class Api:
    async def list_nodes(self, **kwargs):
        if kwargs.get('parent_node_token')=='root':
            return [{'node_token':'child','obj_token':'doc','obj_type':'docx','title':'Child','has_child':False}]
        return [{'node_token':'root','obj_token':'rootdoc','obj_type':'docx','title':'Root','has_child':True}]
    async def get_node(self, **kwargs):
        return {'node_token':'root','space_id':'space','obj_token':'rootdoc','obj_type':'docx','title':'Root','has_child':True}
    async def get_docx_document(self, **kwargs):
        return {'revision_id':7,'title':'Actual title'}
    async def list_docx_blocks(self, **kwargs):
        assert kwargs['document_revision_id']==7
        return [{'block_id':'p','block_type':2,'text':{'elements':[{'text_run':{'content':'FEISHU_SOURCE_614'}}]}}]

@pytest.mark.asyncio
async def test_recursive_root_scope_revision_and_raw_snapshot():
    source=FeishuSource(Api())
    entries=await source.discover(FeishuSelection('wiki','root','space'))
    assert [x.external_id for x in entries]==['wiki:space:root','wiki:space:child']
    assert entries[1].parent_id=='wiki:space:root' and entries[1].path==('Root','Child')
    doc=await source.document(entries[1])
    assert b'FEISHU_SOURCE_614' in doc.raw and b'FEISHU\\_SOURCE\\_614' in doc.markdown
    assert doc.revision=='7'
    assert await source.document(entries[1],previous_revision='7') is None
    with pytest.raises(FeishuSourceError,match='outside'):
        await source.discover(FeishuSelection('wiki','root','another'))

@pytest.mark.asyncio
async def test_cyclic_or_truncated_scope_never_returns_complete_discovery():
    class Cycle(Api):
        async def list_nodes(self, **kwargs):
            return [{'node_token':'root','obj_token':'doc','obj_type':'docx','title':'X','has_child':True}]
    with pytest.raises(FeishuSourceError,match='cyclic'):
        await FeishuSource(Cycle()).discover(FeishuSelection('wiki','','space'))
    with pytest.raises(FeishuSourceError,match='bound'):
        await FeishuSource(Api(),max_entries=1).discover(FeishuSelection('wiki','','space'))

@pytest.mark.asyncio
async def test_drive_folder_bound_and_bitable_no_rows():
    class Drive:
        async def list_drive_files(self, **kwargs):
            return [{'token':'sub','name':'Folder','type':'folder'}]
    with pytest.raises(FeishuSourceError,match='cyclic'):
        await FeishuSource(Drive()).discover(FeishuSelection('drive','root'))
    class Bitable:
        async def list_bitable_tables(self, **kwargs):
            return [{'table_id':'table','name':'Schema'}]
    entries=await FeishuSource(Bitable()).discover(FeishuSelection('bitable','app'))
    assert entries[0].kind=='bitable_table'
    assert entries[0].external_id=='bitable:app:table'
