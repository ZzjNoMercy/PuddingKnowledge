import json
import sqlite3
import sys
import types
from pathlib import Path

import pytest

from knowledge_platform.catalog.sqlite_query import SqliteCatalogQueryRepository
from knowledge_platform.local.index_config import load_index_config, vector_storage


def _config(provider="knowledge_local_vector"):
    value = {
        "version": 1,
        "provider_id": provider,
        "space_ids": ["s"],
        "embedding": {
            "endpoint": "http://127.0.0.1:8080/v1",
            "model": "m",
            "dimension": 2,
            "api_key_env": None,
        },
        "batch_size": 4,
        "max_chars": 1200,
    }
    if provider == "knowledge_milvus_vector":
        value["milvus"] = {
            "endpoint": "https://milvus.example/",
            "api_key_env": "KNOWLEDGE_MILVUS_API_KEY",
        }
    return value


def _write_config(tmp_path: Path, value: dict) -> Path:
    path = tmp_path / "index.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def test_local_config_contract_is_unchanged(tmp_path: Path):
    value = _config()
    assert load_index_config(_write_config(tmp_path, value)) == value


def test_milvus_config_accepts_root_endpoint_and_constructs_storage(tmp_path, monkeypatch):
    value = _config("knowledge_milvus_vector")
    monkeypatch.setenv("KNOWLEDGE_MILVUS_API_KEY", "secret")
    assert load_index_config(_write_config(tmp_path, value)) == value
    made = {}

    class Store:
        def __init__(self, **kwargs):
            made.update(kwargs)

    monkeypatch.setitem(sys.modules, "knowledge_platform.retrieval.milvus_http", types.SimpleNamespace(MilvusVectorStore=Store))
    vector_storage(value)
    assert made == {"endpoint": "https://milvus.example/", "dimension": 2, "api_key": "secret"}


@pytest.mark.parametrize("endpoint", [
    "milvus.example:19530", "ftp://milvus.example", "https://u:p@milvus.example",
    "https://milvus.example/path", "https://milvus.example/?query=1", "https://milvus.example/#fragment",
])
def test_milvus_endpoint_is_explicit_and_root_only(tmp_path: Path, endpoint):
    value = _config("knowledge_milvus_vector")
    value["milvus"]["endpoint"] = endpoint
    with pytest.raises(ValueError):
        load_index_config(_write_config(tmp_path, value))


def test_milvus_credential_reference_is_restricted(tmp_path: Path):
    value = _config("knowledge_milvus_vector")
    value["milvus"]["api_key_env"] = "MILVUS_API_KEY"
    with pytest.raises(ValueError):
        load_index_config(_write_config(tmp_path, value))


def test_catalog_active_provider_defaults_old_schema_and_reads_new_schema(tmp_path: Path):
    path = tmp_path / "catalog.db"
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE knowledge_spaces (id TEXT, name TEXT, description TEXT);
            CREATE TABLE knowledge_datasets (id TEXT, space_id TEXT, name TEXT, version TEXT, kind TEXT,
                capabilities TEXT, freshness TEXT, asset_ids TEXT, semantic_asset_ids TEXT);
            CREATE TABLE knowledge_assets (id TEXT, space_id TEXT, kind TEXT, title TEXT, description TEXT,
                mime_type TEXT, source_type TEXT, source_uri TEXT, revision TEXT, content_digest TEXT);
            CREATE TABLE knowledge_local_vector_indexes (space_id TEXT, collection_id TEXT,
                collection_version TEXT, capability TEXT, status TEXT, provider_id TEXT);
        """)
        db.execute("INSERT INTO knowledge_spaces VALUES ('s','S','')")
        db.execute("INSERT INTO knowledge_datasets VALUES ('c','s','C','1','wiki','[\"wiki_query\"]','{}','[]','[]')")
        db.execute("INSERT INTO knowledge_local_vector_indexes VALUES ('s','c','1','wiki_query','active','knowledge_milvus_vector')")
    snapshot = SqliteCatalogQueryRepository(path).read_package_snapshot()
    assert snapshot.collections[0]["provider_bindings"] == {"wiki_query": {"provider_id": "knowledge_milvus_vector"}}
    with sqlite3.connect(path) as db:
        db.execute("UPDATE knowledge_local_vector_indexes SET provider_id='unknown'")
    with pytest.raises(ValueError):
        SqliteCatalogQueryRepository(path).read_package_snapshot()
    with sqlite3.connect(path) as db:
        db.execute("ALTER TABLE knowledge_local_vector_indexes RENAME TO old_indexes")
        db.execute("CREATE TABLE knowledge_local_vector_indexes (space_id TEXT, collection_id TEXT, collection_version TEXT, capability TEXT, status TEXT)")
        db.execute("INSERT INTO knowledge_local_vector_indexes SELECT space_id, collection_id, collection_version, capability, status FROM old_indexes")
    snapshot = SqliteCatalogQueryRepository(path).read_package_snapshot()
    assert snapshot.collections[0]["provider_bindings"] == {"wiki_query": {"provider_id": "knowledge_local_vector"}}


def test_explicit_no_auth_and_missing_named_secret(tmp_path, monkeypatch):
    value=_config('knowledge_milvus_vector')
    value['milvus']['api_key_env']=None
    assert load_index_config(_write_config(tmp_path,value))==value
    assert vector_storage(value).identity.startswith('sha256:')
    value['milvus']['api_key_env']='KNOWLEDGE_MILVUS_MISSING'
    monkeypatch.delenv('KNOWLEDGE_MILVUS_MISSING',raising=False)
    with pytest.raises(ValueError,match='missing'):vector_storage(value)


@pytest.mark.parametrize('endpoint',['http://localhost\n/path','http://localhost\t','http://localhost:invalid'])
def test_control_characters_and_invalid_port_rejected(tmp_path,endpoint):
    value=_config('knowledge_milvus_vector');value['milvus']['endpoint']=endpoint
    with pytest.raises(ValueError):load_index_config(_write_config(tmp_path,value))
