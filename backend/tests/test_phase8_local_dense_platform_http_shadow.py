from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

from knowledge_platform.catalog.vector_rebuild import VectorRebuildChunk, build_vector_rebuild_manifest


def _load_script():
    path = Path(__file__).parents[1] / "scripts" / "phase8_local_dense_platform_http_shadow.py"
    spec = importlib.util.spec_from_file_location("phase8_local_dense_platform_http_shadow", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _inputs(tmp_path: Path) -> tuple[Path, Path]:
    text = "dense platform query text"
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
        assets=[{"id": "asset-a", "revision": digest, "content_digest": digest}],
    )
    chunk = VectorRebuildChunk(
        asset_id="asset-a",
        chunk_id="asset-a:chunk_1",
        ordinal=1,
        text=text,
        content_digest=digest,
        source_revision=digest,
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
    catalog_revision = "sha256:" + "a" * 64

    def list_collections(self, *, space_id=None):
        return [
            {
                "id": "dataset_kb_default",
                "space_id": "space_kb_default",
                "version": "v1",
                "capabilities": ["document_rag_query"],
                "freshness": {"state": "ready"},
                "asset_ids": ["asset-a"],
                "provider_bindings": {},
            }
        ]

    def get_asset(self, *, asset_id):
        if asset_id == "asset-a":
            return {
                "id": asset_id,
                "space_id": "space_kb_default",
                "revision": "sha256:" + "b" * 64,
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
        assert kwargs["output_fields"] == ["doc_id", "text"]
        return [[{"id": "asset-a:chunk_1", "doc_id": "asset-a", "text": "bound text", "distance": 0.9}]]


class _Embedder:
    provider_version = "test-local-transformers-v1"

    def embed(self, texts):
        assert len(texts) == 1
        return ((0.6, 0.8),)


def test_dense_platform_shadow_reaches_rest_routed_and_mcp_without_binding_catalog(tmp_path: Path) -> None:
    module = _load_script()
    manifest, chunks = _inputs(tmp_path)
    result = module.run_shadow(
        catalog=tmp_path / "catalog.sqlite3",
        manifest_path=manifest,
        chunks_path=chunks,
        model_dir=tmp_path / "model",
        dimension=2,
        source_roots=(),
        milvus_client=_Milvus(),
        catalog_repository=_Catalog(),
        embedding_client=_Embedder(),
        output_dir=tmp_path / "report",
    )

    assert result["status"] == "PHASE8_LOCAL_DENSE_PLATFORM_HTTP_SHADOW_PASS_NOT_ACTIVATABLE"
    assert result["catalog_binding_written"] is False
    assert result["catalog_revision_unchanged"] is True
    assert result["http"]["rest_document"]["evidence_count"] == 1
    assert result["http"]["routed_knowledge"]["status"] == "ok"
    assert result["http"]["mcp_document"]["status"] == "ok"
    assert result["http"]["denied_document"]["error_code"] == "permission_denied"
    report = json.loads((tmp_path / "report" / "phase8-local-dense-platform-http-shadow-report.json").read_text())
    assert "bound text" not in json.dumps(report)


def test_dense_platform_shadow_fails_closed_when_candidate_is_missing(tmp_path: Path) -> None:
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
        source_roots=(),
        milvus_client=_Missing(),
        catalog_repository=_Catalog(),
        embedding_client=_Embedder(),
        output_dir=tmp_path / "report",
    )

    assert result["status"] == "PHASE8_LOCAL_DENSE_PLATFORM_HTTP_SHADOW_BLOCKED"
    assert result["error_type"] == "ValueError"
    assert result["embedding_status"] == "not_attempted"


def test_dense_platform_shadow_rejects_symlinked_output_directory(tmp_path: Path) -> None:
    module = _load_script()
    target = tmp_path / "target"
    target.mkdir()
    output = tmp_path / "report"
    output.symlink_to(target, target_is_directory=True)

    try:
        module.run_shadow(output_dir=output)
    except ValueError as error:
        assert "symlink" in str(error)
    else:  # pragma: no cover - the assertion documents the fail-closed contract
        raise AssertionError("symlinked output directory must be rejected")
