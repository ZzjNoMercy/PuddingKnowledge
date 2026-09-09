import pytest

from knowledge_platform.connector_sync.bitable_reference import (
    BitableReference,
    parse_bitable_reference,
    resolve_bitable_reference,
)
from knowledge_platform.connector_sync.feishu_source import FeishuSourceError


class Api:
    def __init__(self, node=None):
        self.node = node
        self.calls = []

    async def get_node(self, **kwargs):
        self.calls.append(("node", kwargs))
        return self.node

    async def list_bitable_tables(self, **kwargs):
        self.calls.append(("tables", kwargs))
        return [{"table_id": "tbl1", "name": "Orders", "records": [{"secret": 1}]}]


def test_parse_base_and_query_is_strict():
    ref = parse_bitable_reference("https://tenant.feishu.cn/base/app1?table=tbl1&view=vew1")
    assert ref == BitableReference("https://tenant.feishu.cn/base/app1?table=tbl1&view=vew1", "direct_bitable", "app1", "app1", "tbl1", "vew1", "")
    with pytest.raises(FeishuSourceError):
        parse_bitable_reference("https://tenant.feishu.cn/base/app1/extra")
    with pytest.raises(FeishuSourceError):
        parse_bitable_reference("https://tenant.feishu.cn.evil.invalid/base/app1")
    with pytest.raises(FeishuSourceError):
        parse_bitable_reference("https://user:pass@tenant.feishu.cn/base/app1")
    with pytest.raises(FeishuSourceError):
        parse_bitable_reference("https://tenant.feishu.cn:8443/base/app1")
    with pytest.raises(FeishuSourceError):
        parse_bitable_reference("https://tenant.feishu.cn/base/app1?table=tbl1&table=tbl2")
    with pytest.raises(FeishuSourceError):
        parse_bitable_reference("https://tenant.feishu.cn/base/app1?foo=bar")


@pytest.mark.asyncio
async def test_resolve_wiki_requires_bitable_and_never_fetches_rows():
    api = Api({"obj_type": "bitable", "obj_token": "app2"})
    ref, tables = await resolve_bitable_reference(api, "https://tenant.larksuite.com/wiki/node1?table=tbl1")
    assert ref.app_token == "app2" and ref.node_token == "node1"
    assert tables == [{"table_id": "tbl1", "name": "Orders"}]
    assert [kind for kind, _ in api.calls] == ["node", "tables"]
    api.node = {"obj_type": "docx", "obj_token": "app2"}
    with pytest.raises(FeishuSourceError, match="Bitable"):
        await resolve_bitable_reference(api, "https://tenant.larksuite.com/wiki/node1")


@pytest.mark.asyncio
async def test_resolve_rejects_unknown_table_and_malformed_metadata():
    api = Api()
    with pytest.raises(FeishuSourceError, match="not visible"):
        await resolve_bitable_reference(api, "https://tenant.feishu.cn/base/app1?table=missing")
    async def malformed_tables(**kwargs):
        return None
    api.list_bitable_tables = malformed_tables
    with pytest.raises(FeishuSourceError, match="metadata"):
        await resolve_bitable_reference(api, "https://tenant.feishu.cn/base/app1")
