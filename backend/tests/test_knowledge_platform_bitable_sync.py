from __future__ import annotations

import asyncio
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from knowledge_platform.catalog.metadata import KNOWLEDGE_METADATA
from knowledge_platform.catalog.models import KnowledgeAsset, KnowledgeConnector, KnowledgeSourceItem
from knowledge_platform.connector_sync.feishu_source import FeishuSource, FeishuSelection
from knowledge_platform.local.feishu_sync import FeishuSyncService


class BitableApi:
    def __init__(self) -> None:
        self.tables = [{"table_id": "tbl_1", "name": "Cars"}, {"table_id": "tbl_2", "name": "Owners"}]
        self.fields = {
            "tbl_1": [{"field_id": "fld_id", "field_name": "ID", "type": 1, "property": {"ui_type": "Text"}, "is_primary": True}],
            "tbl_2": [{"field_id": "fld_owner", "field_name": "Owner", "type": 1, "property": {"ui_type": "Text"}}],
        }
        self.field_calls = 0

    async def list_bitable_tables(self, *, app_token: str):
        return list(self.tables)

    async def list_bitable_fields(self, *, app_token: str, table_id: str):
        self.field_calls += 1
        return list(self.fields[table_id])


def _fixture(tmp_path: Path, *, relations=None):
    catalog = tmp_path / "catalog.db"
    engine = create_engine(f"sqlite:///{catalog}")
    KNOWLEDGE_METADATA.create_all(engine)
    config = {
        "selection": {"kind": "bitable", "root": "app_1", "wiki_space": ""},
        "bitable": {"relations": relations or []},
    }
    with engine.begin() as connection:
        connection.execute(KnowledgeConnector.__table__.insert().values(
            id="connector_1", space_id="space_1", connector_key="feishu", name="Feishu",
            status="ready", auth_type="builtin", credential_ref="cred_1",
            config_json=config, schedule_json={}, last_error_json={},
        ))
    return catalog, engine


def _items(engine):
    with Session(engine) as session:
        return list(session.scalars(select(KnowledgeSourceItem).order_by(KnowledgeSourceItem.external_id)))


def _assets(engine):
    with Session(engine) as session:
        return list(session.scalars(select(KnowledgeAsset)))


def _sync(service, api, tables, *, key, mode="full", relations=None):
    source = FeishuSource(api, bitable_tables=tables)
    return asyncio.run(service.sync(connector_id="connector_1", space_id="space_1", idempotency_key=key, source=source, mode=mode))


def test_bitable_sync_stores_schema_only_and_zero_records(tmp_path: Path):
    catalog, engine = _fixture(tmp_path)
    service = FeishuSyncService(catalog, tmp_path / "state")
    api = BitableApi()
    try:
        result = _sync(service, api, {"tbl_1": "view_1"}, key="schema-only")
        assert result["linked"] == 1
        items = _items(engine)
        assets = _assets(engine)
        assert len(items) == len(assets) == 1
        assert items[0].status == "linked" and items[0].external_type == "bitable_table"
        assert assets[0].kind == "table_schema" and assets[0].metadata_json["row_storage"] is False
        payload = service.objects.read(assets[0].content_digest)
        assert b"records" not in payload and b"rows" not in payload
        assert b"fld_id" in payload
    finally:
        service.close(); engine.dispose()


def test_bitable_sync_is_stable_and_field_change_creates_new_schema_revision(tmp_path: Path):
    catalog, engine = _fixture(tmp_path)
    service = FeishuSyncService(catalog, tmp_path / "state")
    api = BitableApi()
    try:
        first = _sync(service, api, {"tbl_1": "view_1"}, key="schema-1")
        first_item = _items(engine)[0]
        first_asset = _assets(engine)[0]
        second = _sync(service, api, {"tbl_1": "view_1"}, key="schema-2")
        second_item = _items(engine)[0]
        assert second["unchanged"] == 1 and second["schema_changed"] == 0
        assert second_item.revision == first_item.revision and len(_assets(engine)) == 1
        api.fields["tbl_1"][0]["field_name"] = "VIN"
        third = _sync(service, api, {"tbl_1": "view_1"}, key="schema-3")
        assert third["schema_changed"] == 1
        assert _items(engine)[0].revision != first_item.revision
        assert len(_assets(engine)) == 2 and first_asset.id != _assets(engine)[-1].id
    finally:
        service.close(); engine.dispose()


def test_explicit_empty_scope_discovers_no_tables_and_full_scan_deletes(tmp_path: Path):
    catalog, engine = _fixture(tmp_path)
    service = FeishuSyncService(catalog, tmp_path / "state")
    api = BitableApi()
    try:
        _sync(service, api, {"tbl_1": "view_1", "tbl_2": "view_2"}, key="scope-1")
        result = _sync(service, api, {}, key="scope-2")
        assert result["discovered"] == 0 and result["deleted"] == 2
        assert all(item.status == "deleted" for item in _items(engine))
    finally:
        service.close(); engine.dispose()


def test_table_removed_from_explicit_range_is_deleted_on_full_sync(tmp_path: Path):
    catalog, engine = _fixture(tmp_path)
    service = FeishuSyncService(catalog, tmp_path / "state")
    api = BitableApi()
    try:
        _sync(service, api, {"tbl_1": "view_1", "tbl_2": "view_2"}, key="delete-1")
        api.tables = [{"table_id": "tbl_1", "name": "Cars"}]
        result = _sync(service, api, {"tbl_1": "view_1"}, key="delete-2")
        assert result["deleted"] == 1
        by_id = {item.external_id: item for item in _items(engine)}
        assert by_id["bitable:app_1:tbl_2"].status == "deleted"
    finally:
        service.close(); engine.dispose()


def test_declared_relation_is_persisted_as_stale_when_endpoint_missing(tmp_path: Path):
    relation = {"id": "rel_missing", "name": "Cars to Owners", "description": "join", "cardinality": "many_to_one",
                "source_table_id": "tbl_1", "source_field_id": "fld_missing", "target_table_id": "tbl_2", "target_field_id": "fld_owner"}
    catalog, engine = _fixture(tmp_path, relations=[relation])
    service = FeishuSyncService(catalog, tmp_path / "state")
    api = BitableApi()
    try:
        _sync(service, api, {"tbl_1": "view_1", "tbl_2": "view_2"}, key="relation-stale")
        item = next(item for item in _items(engine) if item.external_id.endswith("tbl_1"))
        stored = next(rel for rel in item.metadata_json["relations"] if rel["id"] == "rel_missing")
        assert stored["validation_status"] == "stale_endpoint"
        assert stored["validation_scope"] == "schema_only"
    finally:
        service.close(); engine.dispose()

