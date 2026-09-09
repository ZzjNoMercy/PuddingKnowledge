from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

from knowledge_platform.catalog.vector_rebuild import VectorRebuildChunk, build_vector_rebuild_manifest


def _load_script():
    path = Path(__file__).parents[1] / "scripts" / "phase8_local_dense_candidate.py"
    spec = importlib.util.spec_from_file_location("phase8_local_dense_candidate", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Schema:
    def __init__(self, **kwargs):
        self.options = kwargs
        self.fields = []

    def add_field(self, **_kwargs):
        self.fields.append(_kwargs)


class _Index:
    def add_index(self, **_kwargs):
        pass


class _Milvus:
    def __init__(self):
        self.rows = []
        self.schema = None
        self.dropped = False

    def has_collection(self, *, collection_name):
        return False

    def create_schema(self, **_kwargs):
        self.schema = _Schema(**_kwargs)
        return self.schema

    def prepare_index_params(self):
        return _Index()

    def create_collection(self, **_kwargs):
        pass

    def insert(self, *, collection_name, data):
        assert collection_name == "puddingclaw_platform_candidate_text"
        self.rows.extend(data)

    def flush(self, *, collection_name):
        assert collection_name == "puddingclaw_platform_candidate_text"

    def load_collection(self, *, collection_name):
        assert collection_name == "puddingclaw_platform_candidate_text"

    def drop_collection(self, *, collection_name):
        assert collection_name == "puddingclaw_platform_candidate_text"
        self.dropped = True

    def get_collection_stats(self, *, collection_name):
        assert collection_name == "puddingclaw_platform_candidate_text"
        return {"row_count": str(len(self.rows))}


class _Embedder:
    def embed(self, texts):
        return tuple((0.1, 0.2) for _ in texts)


def _inputs(tmp_path: Path) -> tuple[Path, Path]:
    source = b"dense candidate text"
    digest = "sha256:" + hashlib.sha256(source).hexdigest()
    manifest = build_vector_rebuild_manifest(
        catalog_revision=digest,
        collection={
            "id": "collection-kb",
            "space_id": "space-kb",
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
        text=source.decode(),
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


def test_dense_candidate_requires_explicit_external_embedding_opt_in(tmp_path: Path) -> None:
    manifest, chunks = _inputs(tmp_path)
    result = _load_script().run_shadow(manifest_path=manifest, chunks_path=chunks, output_dir=tmp_path)
    assert result["status"] == "PHASE8_LOCAL_DENSE_CANDIDATE_BLOCKED_EXTERNAL_EMBEDDING_NOT_AUTHORIZED"
    assert result["embedding_status"] == "not_attempted"


def test_dense_candidate_does_not_default_block_explicit_local_model(tmp_path: Path) -> None:
    module = _load_script()
    result = module.run_shadow(
        manifest_path=tmp_path / "missing-manifest.json",
        chunks_path=tmp_path / "missing-chunks.json",
        output_dir=tmp_path / "reports",
        local_model_dir=tmp_path / "local-model",
        dimension=2048,
    )

    assert result["status"] != "PHASE8_LOCAL_DENSE_CANDIDATE_BLOCKED_EXTERNAL_EMBEDDING_NOT_AUTHORIZED"
    assert result["error_type"] == "FileNotFoundError"


def test_explicit_local_model_branch_builds_candidate_without_network(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_script()

    class _LocalProvider:
        provider_version = "test-local-transformers-v1"

        def __init__(self, **_kwargs):
            pass

        def embed(self, texts):
            return tuple((0.6, 0.8) for _ in texts)

        def close(self):
            pass

    monkeypatch.setattr(module, "LocalTransformersEmbeddingClient", _LocalProvider)
    manifest, chunks = _inputs(tmp_path)
    model_dir = tmp_path / "local-model"
    model_dir.mkdir()
    result = module.run_shadow(
        manifest_path=manifest,
        chunks_path=chunks,
        output_dir=tmp_path / "reports",
        local_model_dir=model_dir,
        dimension=2,
        milvus_client=_Milvus(),
    )

    assert result["status"] == "PHASE8_LOCAL_DENSE_CANDIDATE_PASS_NOT_ACTIVATABLE"
    assert result["embedding_config_source"] == "explicit_local_model"
    assert result["embedding_transport"] == "in_process"
    assert result["network_contacted"] is False
    assert result["external_network_contacted"] is False


def test_dense_candidate_schema_declares_row_provenance_with_dynamic_fields_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_script()

    class _LocalProvider:
        provider_version = "test-local-transformers-v1"

        def __init__(self, **_kwargs):
            pass

        def embed(self, texts):
            return tuple((0.6, 0.8) for _ in texts)

        def close(self):
            pass

    monkeypatch.setattr(module, "LocalTransformersEmbeddingClient", _LocalProvider)
    manifest, chunks = _inputs(tmp_path)
    model_dir = tmp_path / "local-model"
    model_dir.mkdir()
    milvus = _Milvus()
    result = module.run_shadow(
        manifest_path=manifest,
        chunks_path=chunks,
        output_dir=tmp_path / "reports",
        local_model_dir=model_dir,
        dimension=2,
        milvus_client=milvus,
    )

    assert result["status"].endswith("PASS_NOT_ACTIVATABLE")
    assert milvus.schema is not None
    assert milvus.schema.options["enable_dynamic_field"] is False
    assert {field["field_name"] for field in milvus.schema.fields} >= {
        "id",
        "doc_id",
        "text",
        "content_digest",
        "source_revision",
        "embedding",
    }


def test_dense_candidate_insert_failure_rolls_back_created_collection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_script()

    class _LocalProvider:
        provider_version = "test-local-transformers-v1"

        def __init__(self, **_kwargs):
            pass

        def embed(self, texts):
            return tuple((0.6, 0.8) for _ in texts)

        def close(self):
            pass

    class _FailingMilvus(_Milvus):
        def insert(self, *, collection_name, data):
            raise RuntimeError("insert failed")

    monkeypatch.setattr(module, "LocalTransformersEmbeddingClient", _LocalProvider)
    manifest, chunks = _inputs(tmp_path)
    model_dir = tmp_path / "local-model"
    model_dir.mkdir()
    milvus = _FailingMilvus()
    result = module.run_shadow(
        manifest_path=manifest,
        chunks_path=chunks,
        output_dir=tmp_path / "reports",
        local_model_dir=model_dir,
        dimension=2,
        milvus_client=milvus,
    )

    assert result["status"] == "PHASE8_LOCAL_DENSE_CANDIDATE_BLOCKED"
    assert result["error_type"] == "RuntimeError"
    assert result["candidate_collection_rollback"] == "dropped_after_failed_verification"
    assert milvus.dropped is True


def test_dense_candidate_rejects_symlink_input_and_output_boundaries(tmp_path: Path) -> None:
    module = _load_script()
    manifest, chunks = _inputs(tmp_path)
    manifest_link = tmp_path / "manifest-link.json"
    manifest_link.symlink_to(manifest)

    result = module.run_shadow(manifest_path=manifest_link, chunks_path=chunks, output_dir=tmp_path / "reports")
    assert result["error_type"] == "ValueError"
    assert result["network_contacted"] is False

    output_target = tmp_path / "reports-target"
    output_target.mkdir()
    output_link = tmp_path / "reports-link"
    output_link.symlink_to(output_target, target_is_directory=True)
    with pytest.raises(ValueError):
        module.run_shadow(manifest_path=manifest, chunks_path=chunks, output_dir=output_link)


def test_dense_candidate_checkpoint_replay_skips_completed_embedding(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_script()
    calls: list[int] = []

    class _CheckpointProvider:
        provider_version = "test-local-transformers-v1"
        checkpoint_signature = "test-local-transformers-v1|dimension=2"

        def __init__(self, **_kwargs):
            pass

        def embed(self, texts):
            calls.append(len(texts))
            return tuple((0.6, 0.8) for _ in texts)

        def close(self):
            pass

    monkeypatch.setattr(module, "LocalTransformersEmbeddingClient", _CheckpointProvider)
    manifest, chunks = _inputs(tmp_path)
    model_dir = tmp_path / "local-model"
    model_dir.mkdir()
    checkpoint = tmp_path / "embedding-checkpoint.json"

    first = module.run_shadow(
        manifest_path=manifest,
        chunks_path=chunks,
        output_dir=tmp_path / "reports",
        local_model_dir=model_dir,
        dimension=2,
        embedding_checkpoint_path=checkpoint,
        milvus_client=_Milvus(),
    )
    second = module.run_shadow(
        manifest_path=manifest,
        chunks_path=chunks,
        output_dir=tmp_path / "reports-2",
        local_model_dir=model_dir,
        dimension=2,
        embedding_checkpoint_path=checkpoint,
        milvus_client=_Milvus(),
    )

    assert first["status"].endswith("PASS_NOT_ACTIVATABLE")
    assert second["status"].endswith("PASS_NOT_ACTIVATABLE")
    assert first["embedding_checkpoint_rows"] == second["embedding_checkpoint_rows"] == 1
    assert calls == [1]


def test_dense_candidate_milvus_preflight_happens_before_local_model_load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_script()
    manifest, chunks = _inputs(tmp_path)
    model_dir = tmp_path / "local-model"
    model_dir.mkdir()
    load_attempts: list[int] = []

    class _UnavailableMilvus:
        def has_collection(self, *, collection_name):
            raise RuntimeError("milvus preflight unavailable")

    class _UnexpectedProvider:
        provider_version = "unexpected"

        def __init__(self, **_kwargs):
            load_attempts.append(1)
            raise AssertionError("local model should not load before Milvus preflight")

    monkeypatch.setattr(module, "LocalTransformersEmbeddingClient", _UnexpectedProvider)
    result = module.run_shadow(
        manifest_path=manifest,
        chunks_path=chunks,
        output_dir=tmp_path / "reports",
        local_model_dir=model_dir,
        dimension=2,
        milvus_client=_UnavailableMilvus(),
    )

    assert result["status"] == "PHASE8_LOCAL_DENSE_CANDIDATE_BLOCKED"
    assert result["error_type"] == "RuntimeError"
    assert load_attempts == []


def test_local_embedding_endpoint_is_loopback_only() -> None:
    module = _load_script()
    assert module._validate_local_endpoint("http://127.0.0.1:9000/v1/embeddings")
    assert module._validate_local_endpoint("http://localhost:9000/v1/embeddings")
    for endpoint in (
        "https://embedding.example.test/v1/embeddings",
        "http://127.0.0.1:9000/v1/embeddings?key=secret",
        "http://user:pass@127.0.0.1:9000/v1/embeddings",
    ):
        with pytest.raises(ValueError):
            module._validate_local_endpoint(endpoint)


def test_local_embedding_endpoint_does_not_require_external_network_opt_in(tmp_path: Path) -> None:
    manifest, chunks = _inputs(tmp_path)
    milvus = _Milvus()
    result = _load_script().run_shadow(
        manifest_path=manifest,
        chunks_path=chunks,
        output_dir=tmp_path,
        endpoint="http://127.0.0.1:9000/v1/embeddings",
        model="local-model",
        dimension=2,
        local_endpoint=True,
        embed_client=_Embedder(),
        milvus_client=milvus,
    )
    assert result["status"].endswith("PASS_NOT_ACTIVATABLE")
    assert result["external_network_contacted"] is False


def test_local_embedding_endpoint_cannot_be_combined_with_vault_or_external_opt_in(tmp_path: Path) -> None:
    manifest, chunks = _inputs(tmp_path)
    for kwargs in (
        {"allow_network": True},
        {"from_vault": True},
    ):
        result = _load_script().run_shadow(
            manifest_path=manifest,
            chunks_path=chunks,
            output_dir=tmp_path,
            endpoint="http://127.0.0.1:9000/v1/embeddings",
            model="local-model",
            dimension=2,
            local_endpoint=True,
            embed_client=_Embedder(),
            milvus_client=_Milvus(),
            **kwargs,
        )
        assert result["error_type"] == "ValueError"


def test_vault_config_is_not_read_without_network_opt_in(tmp_path: Path, monkeypatch) -> None:
    manifest, chunks = _inputs(tmp_path)

    def forbidden():
        raise AssertionError("Vault must not be read without explicit network opt-in")

    monkeypatch.setitem(sys.modules, "config", types.SimpleNamespace(get_fallback_embedding_config=forbidden))
    result = _load_script().run_shadow(
        manifest_path=manifest,
        chunks_path=chunks,
        output_dir=tmp_path,
        from_vault=True,
    )
    assert result["status"] == "PHASE8_LOCAL_DENSE_CANDIDATE_BLOCKED_EXTERNAL_EMBEDDING_NOT_AUTHORIZED"


def test_dense_candidate_validates_and_writes_injected_embedding_rows(tmp_path: Path) -> None:
    manifest, chunks = _inputs(tmp_path)
    milvus = _Milvus()
    result = _load_script().run_shadow(
        manifest_path=manifest,
        chunks_path=chunks,
        output_dir=tmp_path,
        allow_network=True,
        embed_client=_Embedder(),
        milvus_client=milvus,
        dimension=2,
        batch_size=1,
    )
    assert result["status"].endswith("PASS_NOT_ACTIVATABLE")
    assert result["embedding_status"] == "verified"
    assert result["row_count"] == 1
    assert milvus.rows[0]["id"] == "asset-a:chunk_1"
    assert milvus.rows[0]["doc_id"] == "asset-a"


def test_dense_candidate_rejects_tampered_chunk_provenance(tmp_path: Path) -> None:
    manifest, chunks = _inputs(tmp_path)
    payload = json.loads(chunks.read_text())
    payload["chunks"][0]["content_digest"] = "sha256:" + "0" * 64
    chunks.write_text(json.dumps(payload), encoding="utf-8")
    result = _load_script().run_shadow(
        manifest_path=manifest,
        chunks_path=chunks,
        output_dir=tmp_path,
        allow_network=True,
        embed_client=_Embedder(),
        milvus_client=_Milvus(),
        dimension=2,
    )
    assert result["error_type"] == "VectorRebuildPlanError"
    assert result["embedding_status"] == "not_attempted"
