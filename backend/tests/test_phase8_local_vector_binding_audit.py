from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path


def _load_script():
    path = Path(__file__).parents[1] / "scripts" / "phase8_local_vector_binding_audit.py"
    spec = importlib.util.spec_from_file_location("phase8_local_vector_binding_audit", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE knowledge_datasets (
                id TEXT NOT NULL, space_id TEXT NOT NULL, name TEXT NOT NULL,
                version TEXT NOT NULL, kind TEXT NOT NULL, capabilities TEXT NOT NULL,
                freshness TEXT NOT NULL, asset_ids TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY (space_id, id, version)
            );
            CREATE TABLE knowledge_collection_bindings (
                space_id TEXT NOT NULL, collection_id TEXT NOT NULL, collection_version TEXT NOT NULL,
                capability TEXT NOT NULL, binding_json TEXT NOT NULL, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (space_id, collection_id, collection_version, capability)
            );
            INSERT INTO knowledge_datasets VALUES
                ('dataset_kb', 'space_kb', 'KB', 'v1', 'document-rag',
                 '["document_rag_query"]', '{}', '["asset-a"]', 'now');
            """
        )


def test_audit_report_is_blocked_when_milvus_identity_is_not_catalog_identity(tmp_path: Path, monkeypatch) -> None:
    module = _load_script()
    catalog = tmp_path / "catalog.sqlite3"
    _database(catalog)
    monkeypatch.setattr(module, "_read_vector_document_ids", lambda **_: ["milvus-uuid"])
    output = module.run_audit(
        catalog=catalog,
        vector_uri="http://127.0.0.1:19530",
        vector_collection="text",
        space_id="space_kb",
        collection_id="dataset_kb",
        collection_version="v1",
        output_dir=tmp_path,
    )
    assert output["status"] == "PHASE8_LOCAL_VECTOR_BINDING_AUDIT_BLOCKED"
    assert output["explicit_provider_binding"] is False
    assert output["matched_identity_count"] == 0
    assert output["activation_allowed"] is False


def test_audit_script_reports_provider_unavailable_without_activation(tmp_path: Path, monkeypatch) -> None:
    module = _load_script()
    catalog = tmp_path / "catalog.sqlite3"
    _database(catalog)
    monkeypatch.setattr(module, "_read_vector_document_ids", lambda **_: (_ for _ in ()).throw(ConnectionError("offline")))
    result = module.run_audit(
        catalog=catalog,
        vector_uri="http://127.0.0.1:19530",
        vector_collection="text",
        space_id="space_kb",
        collection_id="dataset_kb",
        collection_version="v1",
        output_dir=tmp_path,
    )
    assert result["status"] == "PHASE8_LOCAL_VECTOR_BINDING_AUDIT_BLOCKED"
    assert result["activation_allowed"] is False
    report = json.loads((tmp_path / "phase8-local-vector-binding-audit-report.json").read_text())
    assert report["error_type"] == "ConnectionError"
