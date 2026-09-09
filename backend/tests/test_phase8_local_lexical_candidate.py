from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
from pathlib import Path


def _load_script():
    path = Path(__file__).parents[1] / "scripts" / "phase8_local_lexical_candidate.py"
    spec = importlib.util.spec_from_file_location("phase8_local_lexical_candidate", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Schema:
    def __init__(self):
        self.fields = []
        self.functions = []

    def add_field(self, **kwargs):
        self.fields.append(kwargs)

    def add_function(self, function):
        self.functions.append(function)


class _IndexParams:
    def __init__(self):
        self.indexes = []

    def add_index(self, **kwargs):
        self.indexes.append(kwargs)


class _Milvus:
    def __init__(self):
        self.rows = []
        self.schema = None
        self.index_params = None
        self.search_calls = []

    def has_collection(self, *, collection_name):
        return False

    def create_schema(self, **_kwargs):
        self.schema = _Schema()
        return self.schema

    def prepare_index_params(self):
        self.index_params = _IndexParams()
        return self.index_params

    def create_collection(self, **kwargs):
        assert kwargs["collection_name"] == "puddingclaw_platform_candidate_lexical_text"

    def insert(self, *, collection_name, data):
        assert collection_name == "puddingclaw_platform_candidate_lexical_text"
        self.rows.extend(data)

    def flush(self, *, collection_name):
        assert collection_name == "puddingclaw_platform_candidate_lexical_text"

    def load_collection(self, *, collection_name):
        assert collection_name == "puddingclaw_platform_candidate_lexical_text"

    def get_collection_stats(self, *, collection_name):
        assert collection_name == "puddingclaw_platform_candidate_lexical_text"
        return {"row_count": str(len(self.rows))}

    def search(self, **kwargs):
        self.search_calls.append(kwargs)
        row = self.rows[0]
        return [[{"entity": row, "distance": 1.0}]]


class _ExistingMilvus(_Milvus):
    def has_collection(self, *, collection_name):
        return True

    def create_schema(self, **_kwargs):  # pragma: no cover - proves no write path is reached
        raise AssertionError("existing candidate must not be modified")


def test_local_lexical_candidate_writes_only_catalog_bound_rows(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source.md"
    source.write_text("PuddingClaw local knowledge", encoding="utf-8")
    digest = "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
    database = tmp_path / "catalog.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE knowledge_spaces (id TEXT, name TEXT, description TEXT);
            CREATE TABLE knowledge_datasets (
                id TEXT NOT NULL, space_id TEXT NOT NULL, name TEXT NOT NULL,
                version TEXT NOT NULL, kind TEXT NOT NULL, capabilities TEXT NOT NULL,
                freshness TEXT NOT NULL, asset_ids TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY (space_id, id, version)
            );
            CREATE TABLE knowledge_assets (
                id TEXT PRIMARY KEY, space_id TEXT NOT NULL, kind TEXT NOT NULL,
                title TEXT NOT NULL, description TEXT NOT NULL, mime_type TEXT NOT NULL,
                source_type TEXT NOT NULL, source_uri TEXT NOT NULL, revision TEXT NOT NULL,
                content_digest TEXT NOT NULL
            );
            CREATE TABLE knowledge_collection_bindings (
                space_id TEXT NOT NULL, collection_id TEXT NOT NULL, collection_version TEXT NOT NULL,
                capability TEXT NOT NULL, binding_json TEXT NOT NULL, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (space_id, collection_id, collection_version, capability)
            );
            INSERT INTO knowledge_spaces VALUES ('space_kb_default', 'KB', '');
            INSERT INTO knowledge_datasets VALUES (
                'dataset_kb_default', 'space_kb_default', 'KB', 'v1', 'document-rag',
                '["document_rag_query"]', '{}', '["asset-doc"]', 'now'
            );
            """,
        )
        connection.execute(
            """INSERT INTO knowledge_assets VALUES (
                'asset-doc', 'space_kb_default', 'document', 'Doc', '', 'text/markdown', 'local',
                'knowledge://spaces/space_kb_default/assets/asset-doc', 'rev-doc', ?
            )""",
            (digest,),
        )
    fake = _Milvus()
    module = _load_script()
    monkeypatch.setattr(module, "MilvusClient", lambda **_kwargs: fake)
    result = module.run_shadow(catalog=database, source_roots=(tmp_path,), output_dir=tmp_path)
    assert result["status"].endswith("PASS_NOT_ACTIVATABLE")
    assert result["row_count"] == result["chunk_count"]
    assert result["search_result_count"] == 1
    assert fake.search_calls[0]["data"][0].startswith("PuddingClaw")
    payload = json.loads((tmp_path / "phase8-local-vector-lexical-manifest.json").read_text())
    assert "source_path" not in json.dumps(payload)
    assert fake.schema.functions[0].name == "puddingclaw_platform_bm25"


def test_local_lexical_candidate_refuses_to_touch_existing_candidate(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source.md"
    source.write_text("PuddingClaw local knowledge", encoding="utf-8")
    digest = "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
    database = tmp_path / "catalog.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE knowledge_spaces (id TEXT, name TEXT, description TEXT);
            CREATE TABLE knowledge_datasets (
                id TEXT NOT NULL, space_id TEXT NOT NULL, name TEXT NOT NULL,
                version TEXT NOT NULL, kind TEXT NOT NULL, capabilities TEXT NOT NULL,
                freshness TEXT NOT NULL, asset_ids TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY (space_id, id, version)
            );
            CREATE TABLE knowledge_assets (
                id TEXT PRIMARY KEY, space_id TEXT NOT NULL, kind TEXT NOT NULL,
                title TEXT NOT NULL, description TEXT NOT NULL, mime_type TEXT NOT NULL,
                source_type TEXT NOT NULL, source_uri TEXT NOT NULL, revision TEXT NOT NULL,
                content_digest TEXT NOT NULL
            );
            CREATE TABLE knowledge_collection_bindings (
                space_id TEXT NOT NULL, collection_id TEXT NOT NULL, collection_version TEXT NOT NULL,
                capability TEXT NOT NULL, binding_json TEXT NOT NULL, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (space_id, collection_id, collection_version, capability)
            );
            INSERT INTO knowledge_spaces VALUES ('space_kb_default', 'KB', '');
            INSERT INTO knowledge_datasets VALUES (
                'dataset_kb_default', 'space_kb_default', 'KB', 'v1', 'document-rag',
                '["document_rag_query"]', '{}', '["asset-doc"]', 'now'
            );
            """
        )
        connection.execute(
            """INSERT INTO knowledge_assets VALUES (
                'asset-doc', 'space_kb_default', 'document', 'Doc', '', 'text/markdown', 'local',
                'knowledge://spaces/space_kb_default/assets/asset-doc', 'rev-doc', ?
            )""",
            (digest,),
        )
    module = _load_script()
    monkeypatch.setattr(module, "MilvusClient", lambda **_kwargs: _ExistingMilvus())
    result = module.run_shadow(catalog=database, source_roots=(tmp_path,), output_dir=tmp_path)
    assert result["status"] == "PHASE8_LOCAL_VECTOR_LEXICAL_CANDIDATE_BLOCKED_EXISTING_COLLECTION"
    assert "row_count" not in result
