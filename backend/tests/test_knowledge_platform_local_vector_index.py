from __future__ import annotations

import asyncio
import json
import hashlib
import sqlite3
from pathlib import Path

import pytest

from knowledge_contracts import BlobReadResult, Correlation, Principal
from knowledge_platform.catalog.index_rebuild import IndexRebuildRequest
from knowledge_platform.local.vector_index import LocalVectorIndex
from knowledge_platform.retrieval.ports import RetrievalIndexNotReady


class Repo:
    def __init__(self):
        self.collection = {"id": "c", "space_id": "s", "version": "1", "capabilities": ["document_rag_query"], "asset_ids": ["a"], "provider_bindings": {"document_rag_query": {"provider_id": "knowledge_local_vector"}}}
        digest = "sha256:" + hashlib.sha256(b"alpha document\nbeta document").hexdigest()
        self.asset = {"id": "a", "space_id": "s", "kind": "document", "mime_type": "text/plain", "source_uri": "knowledge://spaces/s/assets/a", "content_digest": digest}
    def list_collections(self, *, space_id): return [self.collection] if space_id == "s" else []
    def list_assets(self, *, space_id): return [self.asset] if space_id == "s" else []


class Reader:
    async def read(self, request):
        body = b"alpha document\nbeta document"
        digest = "sha256:" + hashlib.sha256(body).hexdigest()
        return BlobReadResult(request.resource_uri, body, digest, 0, len(body), digest)


class Embedder:
    def __init__(self): self.calls = 0; self.cancel = False; self.fail = False
    def embed(self, texts):
        self.calls += 1
        if self.cancel: raise asyncio.CancelledError()
        if self.fail: raise RuntimeError("embedding failed")
        return tuple((float(len(text)), 1.0) for text in texts)


def _index(tmp_path: Path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    repo, embedder = Repo(), Embedder()
    index = LocalVectorIndex(tmp_path / "catalog.db", repo, Reader(), embedder, {"version": 1, "provider_id": "knowledge_local_vector", "space_ids": ["s"], "embedding": {"endpoint": "http://127.0.0.1:1", "model": "m", "dimension": 2}, "batch_size": 8, "max_chars": 1200})
    with sqlite3.connect(tmp_path / "catalog.db") as db:
        db.execute("CREATE TABLE IF NOT EXISTS knowledge_collection_bindings (space_id TEXT, collection_id TEXT, collection_version TEXT, capability TEXT, binding_json TEXT, created_at TEXT, updated_at TEXT, PRIMARY KEY(space_id,collection_id,collection_version,capability))")
    principal = Principal("admin", scopes=("knowledge.admin", "knowledge.space:s"))
    request = IndexRebuildRequest("s", "c", "1", "document_rag_query", "knowledge_local_vector", "key")
    return index, repo, embedder, principal, request


def test_rebuild_and_cosine_search_use_real_sqlite(tmp_path: Path):
    index, _repo, embedder, principal, request = _index(tmp_path)
    result = asyncio.run(index.rebuild(principal, Correlation("trace"), request))
    assert result.status == "ok"
    assert result.data["index"]["generation"] == 1
    assert result.data["index"]["chunk_count"] == 1
    assert asyncio.run(index.search(query="alpha", space_id="s", limit=5))[0].asset_id == "a"
    assert embedder.calls == 2
    with sqlite3.connect(tmp_path / "catalog.db") as db:
        assert db.execute("SELECT status FROM knowledge_local_vector_indexes").fetchone()[0] == "active"


def test_same_key_replay_does_not_embed_and_changed_key_is_rejected(tmp_path: Path):
    index, repo, embedder, principal, request = _index(tmp_path)
    asyncio.run(index.rebuild(principal, Correlation("trace"), request)); calls = embedder.calls
    replay = asyncio.run(index.rebuild(principal, Correlation("trace2"), request))
    assert replay.data["index"]["idempotent"] is True and embedder.calls == calls
    repo.collection["asset_ids"] = []
    changed = asyncio.run(index.rebuild(principal, Correlation("trace3"), request))
    assert changed.status == "error"


def test_forged_chunks_and_source_change_are_not_ready(tmp_path: Path):
    index, repo, _embedder, principal, request = _index(tmp_path)
    asyncio.run(index.rebuild(principal, Correlation("trace"), request))
    with sqlite3.connect(tmp_path / "catalog.db") as db:
        db.execute("UPDATE knowledge_local_vector_chunks SET text='forged'"); db.commit()
    with pytest.raises(RetrievalIndexNotReady): asyncio.run(index.search(query="alpha", space_id="s", limit=5))
    # Restore the index, then mutate the authoritative Catalog asset digest.
    index, repo, _embedder, principal, request = _index(tmp_path / "second")
    asyncio.run(index.rebuild(principal, Correlation("trace"), request)); repo.asset["content_digest"] = "sha256:" + "2" * 64
    with pytest.raises(RetrievalIndexNotReady): asyncio.run(index.search(query="alpha", space_id="s", limit=5))


def test_failed_or_cancelled_embedding_preserves_previous_generation(tmp_path: Path):
    index, _repo, embedder, principal, request = _index(tmp_path)
    asyncio.run(index.rebuild(principal, Correlation("trace"), request))
    embedder.fail = True
    failed = asyncio.run(index.rebuild(principal, Correlation("trace2"), IndexRebuildRequest("s", "c", "1", "document_rag_query", "knowledge_local_vector", "new")))
    assert failed.status == "error"
    with sqlite3.connect(tmp_path / "catalog.db") as db: assert db.execute("SELECT COUNT(*) FROM knowledge_local_vector_indexes WHERE status='active'").fetchone()[0] == 1


def test_cancel_and_commit_failure_do_not_replace_old_generation(tmp_path: Path):
    index, _repo, embedder, principal, request = _index(tmp_path)
    asyncio.run(index.rebuild(principal, Correlation("trace"), request))
    embedder.cancel = True
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(index.rebuild(principal, Correlation("cancel"), IndexRebuildRequest("s", "c", "1", "document_rag_query", "knowledge_local_vector", "cancel")))
    index._before_commit = lambda _db: (_ for _ in ()).throw(RuntimeError("commit fault"))
    embedder.cancel = False
    failed = asyncio.run(index.rebuild(principal, Correlation("fault"), IndexRebuildRequest("s", "c", "1", "document_rag_query", "knowledge_local_vector", "fault")))
    assert failed.status == "error"
    with sqlite3.connect(tmp_path / "catalog.db") as db:
        assert db.execute("SELECT COUNT(*) FROM knowledge_local_vector_indexes WHERE status='active'").fetchone()[0] == 1
