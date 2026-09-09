from __future__ import annotations

import pytest

from scripts.phase8_local_dense_reuse_audit import _require_loopback, summarize_legacy_rows


def _chunk(chunk_id: str, asset_id: str, text: str) -> dict[str, object]:
    return {"chunk_id": chunk_id, "asset_id": asset_id, "text": text}


def test_dense_reuse_summary_rejects_unrelated_legacy_identity() -> None:
    summary = summarize_legacy_rows(
        chunks=[_chunk("chunk-1", "asset-1", "alpha")],
        rows=[{"id": "old-1", "doc_id": "old-asset", "text": "different"}],
        legacy_row_count=1,
        embedding_dimension=1024,
    )

    assert summary["document_id_intersection"] == 0
    assert summary["document_id_text_digest_intersection"] == 0
    assert summary["reuse_compatible"] is False
    assert summary["reuse_allowed"] is False


def test_dense_reuse_summary_still_never_authorizes_reuse() -> None:
    summary = summarize_legacy_rows(
        chunks=[_chunk("chunk-1", "asset-1", "alpha")],
        rows=[{"id": "chunk-1", "doc_id": "asset-1", "text": "alpha"}],
        legacy_row_count=1,
        embedding_dimension=1024,
    )

    assert summary["reuse_compatible"] is True
    assert summary["reuse_allowed"] is False


def test_dense_reuse_summary_rejects_incomplete_observation() -> None:
    summary = summarize_legacy_rows(
        chunks=[_chunk("chunk-1", "asset-1", "alpha")],
        rows=[{"id": "chunk-1", "doc_id": "asset-1", "text": "alpha"}],
        legacy_row_count=2,
        embedding_dimension=1024,
    )

    assert summary["observation_complete"] is False
    assert summary["reuse_compatible"] is False


def test_dense_reuse_summary_rejects_extra_or_duplicate_legacy_rows() -> None:
    row = {"id": "chunk-1", "doc_id": "asset-1", "text": "alpha"}
    summary = summarize_legacy_rows(
        chunks=[_chunk("chunk-1", "asset-1", "alpha")],
        rows=[row, {**row, "text": "duplicate"}],
        legacy_row_count=2,
        embedding_dimension=1024,
    )

    assert summary["observation_complete"] is True
    assert summary["legacy_unique_ids"] == 1
    assert summary["reuse_compatible"] is False


def test_dense_reuse_audit_is_loopback_only() -> None:
    assert _require_loopback("http://127.0.0.1:19530")
    with pytest.raises(ValueError):
        _require_loopback("https://milvus.example.test:19530")
