from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from pathlib import Path

import pytest

from knowledge_contracts import BlobReadResult, Correlation, Principal
from knowledge_platform.catalog.index_rebuild import IndexRebuildRequest
from knowledge_platform.local.vector_index import LocalVectorIndex
from knowledge_platform.retrieval.ports import RetrievalIndexNotReady


def digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


class Repo:
    def __init__(self):
        self.text = b"catalog text"
        self.image = b"image bytes"
        self.assets = [
            {"id": "doc", "space_id": "s", "kind": "document", "mime_type": "text/plain", "title": "Doc", "description": "text", "source_uri": "knowledge://spaces/s/assets/doc", "content_digest": digest(self.text)},
            {"id": "img", "space_id": "s", "kind": "original_file", "mime_type": "image/png", "title": "Product photo", "description": "red product", "source_uri": "knowledge://spaces/s/assets/img", "content_digest": digest(self.image)},
        ]
        self.collection = {"id": "c", "space_id": "s", "version": "1", "capabilities": ["document_rag_query"], "asset_ids": ["doc", "img"], "provider_bindings": {"document_rag_query": {"provider_id": "knowledge_local_vector"}}}
    def list_collections(self, *, space_id): return [self.collection]
    def list_assets(self, *, space_id): return self.assets


class Reader:
    def __init__(self, repo): self.repo = repo; self.revoked = False
    async def read(self, request):
        if self.revoked: raise RuntimeError("source revoked")
        body = self.repo.text if request.resource_uri.endswith("doc") else self.repo.image
        return BlobReadResult(request.resource_uri, body, digest(body), 0, len(body), digest(body))


class MMEmbedder:
    def __init__(self): self.text_calls = []; self.image_calls = []; self.fail_images = False
    def embed(self, texts): self.text_calls.append(list(texts)); return tuple((1.0, 0.0) for _ in texts)
    def embed_images(self, images):
        if self.fail_images: raise RuntimeError("image source revoked")
        self.image_calls.append([(bytes(data), mime) for data, mime in images]); return tuple((0.0, 1.0) for _ in images)


def make_index(tmp_path: Path, *, protocol: str | None = "dashscope_multimodal"):
    tmp_path.mkdir(parents=True, exist_ok=True); repo = Repo(); reader = Reader(repo); embedder = MMEmbedder()
    embedding = {"endpoint": "http://127.0.0.1:1", "model": "m", "dimension": 2}
    if protocol is not None: embedding["protocol"] = protocol
    index = LocalVectorIndex(tmp_path / "catalog.db", repo, reader, embedder, {"version": 1, "provider_id": "knowledge_local_vector", "space_ids": ["s"], "embedding": embedding, "batch_size": 8, "max_chars": 1200})
    with sqlite3.connect(tmp_path / "catalog.db") as db:
        db.execute("CREATE TABLE IF NOT EXISTS knowledge_collection_bindings (space_id TEXT, collection_id TEXT, collection_version TEXT, capability TEXT, binding_json TEXT, created_at TEXT, updated_at TEXT, PRIMARY KEY(space_id,collection_id,collection_version,capability))")
    return index, repo, reader, embedder, Principal("admin", scopes=("knowledge.admin", "knowledge.space:s")), IndexRebuildRequest("s", "c", "1", "document_rag_query", "knowledge_local_vector", "k")


def test_multimodal_image_embedding_and_empty_image_quote(tmp_path: Path):
    index, _repo, _reader, embedder, principal, request = make_index(tmp_path)
    result = asyncio.run(index.rebuild(principal, Correlation("t"), request))
    assert result.status == "ok"
    assert embedder.image_calls and embedder.image_calls[0][0][1] == "image/png"
    # Image evidence must retain URI identity without leaking binary content.
    rows = asyncio.run(index.search(query="product", space_id="s", limit=5))
    image_rows = [row for row in rows if row.asset_id == "img"]
    assert image_rows and image_rows[0].quote == ""


def test_without_multimodal_protocol_image_is_ignored(tmp_path: Path):
    index, _repo, _reader, embedder, principal, request = make_index(tmp_path, protocol=None)
    asyncio.run(index.rebuild(principal, Correlation("t"), request))
    assert embedder.image_calls == []


def test_image_source_revocation_during_embedding_does_not_publish(tmp_path: Path):
    index, _repo, reader, embedder, principal, request = make_index(tmp_path)
    original_embed = embedder.embed_images
    def revoke(images):
        result = original_embed(images); reader.revoked = True; return result
    embedder.embed_images = revoke
    result = asyncio.run(index.rebuild(principal, Correlation("t"), request))
    assert result.status == "error"
    with sqlite3.connect(tmp_path / "catalog.db") as db:
        assert db.execute("SELECT COUNT(*) FROM knowledge_local_vector_indexes WHERE status='active'").fetchone()[0] == 0


def test_image_title_change_makes_existing_index_stale(tmp_path: Path):
    index, repo, _reader, _embedder, principal, request = make_index(tmp_path)
    asyncio.run(index.rebuild(principal, Correlation("t"), request)); repo.assets[1]["title"] = "Changed title"
    with pytest.raises(RetrievalIndexNotReady): asyncio.run(index.search(query="product", space_id="s", limit=5))


def test_protocol_change_does_not_reuse_old_index(tmp_path: Path):
    index, repo, reader, embedder, principal, request = make_index(tmp_path, protocol="dashscope_multimodal")
    asyncio.run(index.rebuild(principal, Correlation("t"), request))
    changed = LocalVectorIndex(tmp_path / "catalog.db", repo, reader, embedder, {"version": 1, "provider_id": "knowledge_local_vector", "space_ids": ["s"], "embedding": {"endpoint": "http://127.0.0.1:1", "model": "m", "dimension": 2, "protocol": "openai"}, "batch_size": 8, "max_chars": 1200})
    with pytest.raises(RetrievalIndexNotReady): asyncio.run(changed.search(query="product", space_id="s", limit=5))


def test_multimodal_config_uses_explicit_protocol_and_credentials(tmp_path, monkeypatch):
    import json
    from knowledge_platform.local.index_config import load_index_config, embedding_client
    from knowledge_platform.retrieval.multimodal_embedding import DashScopeMultimodalEmbeddingClient
    config = {"version":1, "provider_id":"knowledge_local_vector", "space_ids":["s"],
        "batch_size":16, "max_chars":1200, "embedding":{"protocol":"dashscope_multimodal",
        "endpoint":"http://127.0.0.1:8080/embeddings", "model":"qwen3-vl-embedding", "dimension":2,
        "api_key_env":"KNOWLEDGE_EMBEDDING_MISSING_MM"}}
    path = tmp_path / "config.json"; path.write_text(json.dumps(config))
    loaded = load_index_config(path)
    monkeypatch.delenv("KNOWLEDGE_EMBEDDING_MISSING_MM", raising=False)
    with pytest.raises(ValueError, match="missing"): embedding_client(loaded)
    monkeypatch.setenv("KNOWLEDGE_EMBEDDING_MISSING_MM", "fixture")
    assert isinstance(embedding_client(loaded), DashScopeMultimodalEmbeddingClient)
    config["embedding"]["protocol"] = "unknown"
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError, match="protocol"): load_index_config(path)


def test_image_only_collection_can_be_indexed(tmp_path):
    index, repo, _reader, embedder, principal, request = make_index(tmp_path)
    repo.assets = repo.assets[1:]; repo.collection["asset_ids"] = ["img"]
    result = asyncio.run(index.rebuild(principal, Correlation("t"), request))
    assert result.status == "ok" and embedder.text_calls == []
    rows = asyncio.run(index.search(query="product", space_id="s", limit=1))
    assert len(rows) == 1 and rows[0].asset_id == "img" and rows[0].quote == ""
