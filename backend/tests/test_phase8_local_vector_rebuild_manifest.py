from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path


def _load_script():
    path = Path(__file__).parents[1] / "scripts" / "phase8_local_vector_rebuild_manifest.py"
    spec = importlib.util.spec_from_file_location("phase8_local_vector_rebuild_manifest", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_shadow_writes_path_free_stable_manifest_and_separates_structured_assets(tmp_path: Path) -> None:
    database = tmp_path / "catalog.sqlite3"
    source = tmp_path / "source.md"
    source.write_text("hello", encoding="utf-8")
    import hashlib

    digest = "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
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
            INSERT INTO knowledge_datasets VALUES
                ('dataset_kb_default', 'space_kb_default', 'KB', 'v1', 'document-rag',
                 '["document_rag_query"]', '{}', '["asset-doc", "asset-table"]', 'now');
            """,
        )
        connection.executemany(
            "INSERT INTO knowledge_assets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    "asset-doc", "space_kb_default", "document", "Doc", "", "text/markdown", "local",
                    "knowledge://spaces/space_kb_default/assets/asset-doc", "rev-doc", digest,
                ),
                (
                    "asset-table", "space_kb_default", "document", "Table", "",
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "local",
                    "knowledge://spaces/space_kb_default/assets/asset-table", "rev-table",
                    "sha256:" + "0" * 64,
                ),
            ],
        )
    module = _load_script()
    result = module.run_shadow(catalog=database, source_roots=(tmp_path,), output_dir=tmp_path)
    assert result["status"].endswith("PASS_NOT_ACTIVATABLE")
    assert result["document_asset_count"] == 1
    assert result["structured_asset_count"] == 1
    manifest_path = tmp_path / "phase8-local-vector-rebuild-manifest.json"
    assert manifest_path.exists()
    assert len(json.loads(manifest_path.read_text(encoding="utf-8"))["documents"]) == 1

    # A complete source set yields a candidate manifest without physical paths.
    table = tmp_path / "table.xlsx"
    table.write_bytes(b"table")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE knowledge_assets SET content_digest = ? WHERE id = 'asset-table'",
            ("sha256:" + hashlib.sha256(table.read_bytes()).hexdigest(),),
        )
    result = module.run_shadow(catalog=database, source_roots=(tmp_path,), output_dir=tmp_path)
    assert result["status"].endswith("PASS_NOT_ACTIVATABLE")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert len(manifest["documents"]) == 1
    assert "source_path" not in json.dumps(manifest)
