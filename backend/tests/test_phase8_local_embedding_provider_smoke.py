from __future__ import annotations

import json
from pathlib import Path

from scripts.phase8_local_embedding_provider_smoke import run_smoke


class _Embedder:
    def embed(self, texts):
        return tuple((0.6, 0.8) for _ in texts)

    def close(self):
        pass


def _write_chunks(path: Path, texts: list[str]) -> None:
    path.write_text(json.dumps({"chunks": [{"text": text} for text in texts]}), encoding="utf-8")


def test_provider_smoke_is_bounded_and_never_creates_collection(tmp_path: Path) -> None:
    chunks = tmp_path / "chunks.json"
    _write_chunks(chunks, ["alpha", "beta", "gamma"])

    result = run_smoke(
        model_dir=tmp_path / "unused-model",
        chunks_path=chunks,
        output_dir=tmp_path / "reports",
        dimension=2,
        limit=2,
        embed_client=_Embedder(),
    )

    assert result["status"] == "PHASE8_LOCAL_DENSE_PROVIDER_SMOKE_PASS_NOT_ACTIVATABLE"
    assert result["total_chunk_count"] == 3
    assert result["smoke_chunk_count"] == 2
    assert result["vector_count"] == 2
    assert result["candidate_collection_created"] is False
    assert result["network_contacted"] is False
    assert result["vector_values_emitted"] is False


def test_provider_smoke_rejects_unbounded_limit(tmp_path: Path) -> None:
    chunks = tmp_path / "chunks.json"
    _write_chunks(chunks, ["alpha"])

    result = run_smoke(
        model_dir=tmp_path / "unused-model",
        chunks_path=chunks,
        output_dir=tmp_path / "reports",
        dimension=2,
        limit=33,
        embed_client=_Embedder(),
    )

    assert result["status"] == "PHASE8_LOCAL_DENSE_PROVIDER_SMOKE_FAILED"
    assert result["error_type"] == "ValueError"
    assert result["activation_allowed"] is False
