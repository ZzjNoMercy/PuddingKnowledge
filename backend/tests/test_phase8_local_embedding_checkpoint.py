from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from knowledge_platform.catalog.vector_rebuild import (
    VectorRebuildChunk,
    VectorRebuildPlanError,
    build_embedded_rows_checkpointed,
)


def _chunks() -> tuple[VectorRebuildChunk, ...]:
    digest = "sha256:" + hashlib.sha256(b"source").hexdigest()
    return tuple(
        VectorRebuildChunk(
            asset_id="asset-a",
            chunk_id=f"asset-a:chunk_{ordinal}",
            ordinal=ordinal,
            text=text,
            content_digest=digest,
            source_revision="rev-a",
        )
        for ordinal, text in enumerate(("first", "second"), start=1)
    )


def test_checkpoint_is_atomic_and_resumes_only_missing_batches(tmp_path: Path) -> None:
    chunks = _chunks()
    checkpoint = tmp_path / "embedding-checkpoint.json"
    calls = 0

    def flaky_embed(texts):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected provider failure")
        return ((0.6, 0.8),)

    with pytest.raises(VectorRebuildPlanError):
        build_embedded_rows_checkpointed(
            chunks=chunks,
            embed=flaky_embed,
            dimension=2,
            batch_size=1,
            checkpoint_path=checkpoint,
            manifest_digest="sha256:" + "a" * 64,
            provider_signature="local-transformers-v1|dimension=2",
        )

    first_payload = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert len(first_payload["rows"]) == 1
    assert "text" not in checkpoint.read_text(encoding="utf-8")

    calls = 0
    rows = build_embedded_rows_checkpointed(
        chunks=chunks,
        embed=lambda texts: (tuple((0.6, 0.8) for _ in texts)),
        dimension=2,
        batch_size=1,
        checkpoint_path=checkpoint,
        manifest_digest="sha256:" + "a" * 64,
        provider_signature="local-transformers-v1|dimension=2",
    )

    assert len(rows) == 2
    assert [row.chunk.chunk_id for row in rows] == [chunk.chunk_id for chunk in chunks]
    assert len(json.loads(checkpoint.read_text(encoding="utf-8"))["rows"]) == 2


def test_checkpoint_identity_and_provenance_are_fail_closed(tmp_path: Path) -> None:
    chunks = _chunks()
    checkpoint = tmp_path / "embedding-checkpoint.json"
    checkpoint.write_text(
        json.dumps(
            {
                "format": "agent-knowledge-platform-vector-embedding-checkpoint/v1",
                "manifest_digest": "sha256:" + "b" * 64,
                "provider_signature": "other-provider",
                "dimension": 2,
                "rows": [],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(VectorRebuildPlanError):
        build_embedded_rows_checkpointed(
            chunks=chunks,
            embed=lambda texts: ((0.6, 0.8),),
            dimension=2,
            batch_size=1,
            checkpoint_path=checkpoint,
            manifest_digest="sha256:" + "a" * 64,
            provider_signature="local-transformers-v1|dimension=2",
        )


def test_checkpoint_rejects_boolean_dimension_identity(tmp_path: Path) -> None:
    chunks = _chunks()
    checkpoint = tmp_path / "embedding-checkpoint.json"
    checkpoint.write_text(
        json.dumps(
            {
                "format": "agent-knowledge-platform-vector-embedding-checkpoint/v1",
                "manifest_digest": "sha256:" + "a" * 64,
                "provider_signature": "local-transformers-v1|dimension=1",
                "dimension": True,
                "rows": [],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(VectorRebuildPlanError):
        build_embedded_rows_checkpointed(
            chunks=chunks,
            embed=lambda texts: ((0.6,),),
            dimension=1,
            batch_size=1,
            checkpoint_path=checkpoint,
            manifest_digest="sha256:" + "a" * 64,
            provider_signature="local-transformers-v1|dimension=1",
        )


def test_checkpoint_rejects_path_like_provider_signature(tmp_path: Path) -> None:
    with pytest.raises(VectorRebuildPlanError):
        build_embedded_rows_checkpointed(
            chunks=_chunks(),
            embed=lambda texts: ((0.6, 0.8),),
            dimension=2,
            batch_size=1,
            checkpoint_path=tmp_path / "checkpoint.json",
            manifest_digest="sha256:" + "a" * 64,
            provider_signature="local/secret",
        )


def test_checkpoint_rejects_symlink_path(tmp_path: Path) -> None:
    chunks = _chunks()
    target = tmp_path / "checkpoint-target.json"
    target.write_text("{}", encoding="utf-8")
    link = tmp_path / "checkpoint-link.json"
    link.symlink_to(target)

    with pytest.raises(VectorRebuildPlanError):
        build_embedded_rows_checkpointed(
            chunks=chunks,
            embed=lambda texts: ((0.6, 0.8),),
            dimension=2,
            batch_size=1,
            checkpoint_path=link,
            manifest_digest="sha256:" + "a" * 64,
            provider_signature="local-transformers-v1|dimension=2",
        )
