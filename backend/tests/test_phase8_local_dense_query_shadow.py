from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

from knowledge_platform.catalog.vector_rebuild import VectorRebuildChunk, build_vector_rebuild_manifest


def _load_script():
    path = Path(__file__).parents[1] / "scripts" / "phase8_local_dense_query_shadow.py"
    spec = importlib.util.spec_from_file_location("phase8_local_dense_query_shadow", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _inputs(tmp_path: Path) -> tuple[Path, Path]:
    text = "dense query text"
    digest = "sha256:" + hashlib.sha256(text.encode()).hexdigest()
    manifest = build_vector_rebuild_manifest(
        catalog_revision=digest,
        collection={
            "id": "collection-kb",
            "space_id": "space_kb_default",
            "version": "v1",
            "capabilities": ["document_rag_query"],
            "asset_ids": ["asset-a"],
        },
        assets=[{"id": "asset-a", "revision": "rev-a", "content_digest": digest}],
    )
    chunk = VectorRebuildChunk(
        asset_id="asset-a",
        chunk_id="asset-a:chunk_1",
        ordinal=1,
        text=text,
        content_digest=digest,
        source_revision="rev-a",
    )
    manifest_path = tmp_path / "manifest.json"
    chunks_path = tmp_path / "chunks.json"
    manifest_path.write_text(json.dumps(manifest.to_dict()), encoding="utf-8")
    chunks_path.write_text(
        json.dumps(
            {
                "manifest_digest": manifest.manifest_digest(),
                "provider_collection_name": manifest.provider_collection_name,
                "chunks": [chunk.to_dict()],
            }
        ),
        encoding="utf-8",
    )
    return manifest_path, chunks_path


class _Catalog:
    catalog_revision = "revision-a"

    def get_asset(self, *, asset_id: str):
        if asset_id == "asset-a":
            return {
                "id": asset_id,
                "space_id": "space_kb_default",
                "source_uri": "knowledge://spaces/space_kb_default/assets/asset-a",
            }
        return None


class _Milvus:
    def has_collection(self, *, collection_name):
        assert collection_name == "puddingclaw_platform_candidate_text"
        return True

    def get_collection_stats(self, *, collection_name):
        assert collection_name == "puddingclaw_platform_candidate_text"
        return {"row_count": 1}

    def search(self, **kwargs):
        assert kwargs["collection_name"] == "puddingclaw_platform_candidate_text"
        assert kwargs["output_fields"] == ["doc_id", "text"]
        return [[{"id": "asset-a:chunk_1", "doc_id": "asset-a", "text": "bound text", "distance": 0.9}]]


class _Embedder:
    provider_version = "test-local-transformers-v1"

    def embed(self, texts):
        assert len(texts) == 1
        return ((0.6, 0.8),)


def test_dense_query_shadow_uses_candidate_and_suppresses_query_and_quote(tmp_path: Path) -> None:
    module = _load_script()
    manifest, chunks = _inputs(tmp_path)
    result = module.run_shadow(
        catalog=tmp_path / "catalog.sqlite3",
        manifest_path=manifest,
        chunks_path=chunks,
        model_dir=tmp_path / "model",
        dimension=2,
        milvus_client=_Milvus(),
        catalog_repository=_Catalog(),
        embedding_client=_Embedder(),
        output_dir=tmp_path / "report",
    )

    assert result["status"] == "PHASE8_LOCAL_DENSE_QUERY_SHADOW_PASS_NOT_ACTIVATABLE"
    assert result["result_count"] == 1
    assert result["candidate_row_count"] == 1
    assert result["catalog_revision_unchanged"] is True
    assert result["query_text_emitted"] is False
    assert result["source_paths_emitted"] is False
    report = json.loads((tmp_path / "report" / "phase8-local-dense-query-shadow-report.json").read_text())
    assert "bound text" not in json.dumps(report)


def test_dense_query_shadow_fails_closed_when_candidate_is_missing(tmp_path: Path) -> None:
    module = _load_script()
    manifest, chunks = _inputs(tmp_path)

    class _Missing(_Milvus):
        def has_collection(self, *, collection_name):
            return False

    result = module.run_shadow(
        catalog=tmp_path / "catalog.sqlite3",
        manifest_path=manifest,
        chunks_path=chunks,
        model_dir=tmp_path / "model",
        dimension=2,
        milvus_client=_Missing(),
        catalog_repository=_Catalog(),
        embedding_client=_Embedder(),
        output_dir=tmp_path / "report",
    )

    assert result["status"] == "PHASE8_LOCAL_DENSE_QUERY_SHADOW_BLOCKED"
    assert result["error_type"] == "ValueError"
    assert result["embedding_status"] == "not_attempted"
