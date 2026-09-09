from __future__ import annotations

import hashlib
import sys
import types

import pytest

from knowledge_platform.catalog.vector_rebuild import (
    VectorRebuildPlanError,
    build_embedded_rows,
    build_text_chunks,
    build_vector_rebuild_manifest,
)


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _manifest():
    source = ("# Title\n\n" + ("paragraph with stable content.\n\n" * 20)).encode()
    return build_vector_rebuild_manifest(
        catalog_revision=_digest(b"catalog"),
        collection={
            "id": "collection-kb",
            "space_id": "space-kb",
            "version": "v1",
            "capabilities": ["document_rag_query"],
            "asset_ids": ["asset-a"],
        },
        assets=[{"id": "asset-a", "revision": "rev-a", "content_digest": _digest(source)}],
    ), source


def test_text_chunks_are_deterministic_and_path_free() -> None:
    manifest, source = _manifest()
    chunks = build_text_chunks(manifest=manifest, source_bytes={"asset-a": source}, max_chars=200)
    assert len(chunks) > 1
    assert chunks[0].chunk_id == "asset-a:chunk_1"
    assert all(chunk.asset_id == chunk.to_dict()["provider_document_id"] for chunk in chunks)
    assert "/" not in str(chunks[0].to_dict())


def test_text_chunks_reject_source_digest_mismatch_or_missing_asset() -> None:
    manifest, source = _manifest()
    with pytest.raises(VectorRebuildPlanError, match="digest"):
        build_text_chunks(manifest=manifest, source_bytes={"asset-a": b"changed"})
    with pytest.raises(VectorRebuildPlanError, match="source map"):
        build_text_chunks(manifest=manifest, source_bytes={})


def test_text_chunks_reject_unsafe_control_characters() -> None:
    source = b"hello\x00world"
    manifest = build_vector_rebuild_manifest(
        catalog_revision=_digest(b"catalog"),
        collection={
            "id": "collection-kb",
            "space_id": "space-kb",
            "version": "v1",
            "capabilities": ["document_rag_query"],
            "asset_ids": ["asset-a"],
        },
        assets=[{"id": "asset-a", "revision": "rev-a", "content_digest": _digest(source)}],
    )
    with pytest.raises(VectorRebuildPlanError, match="control"):
        build_text_chunks(manifest=manifest, source_bytes={"asset-a": source})


def test_text_chunks_extract_pdf_without_changing_source_digest(monkeypatch) -> None:
    source = b"%PDF-1.7\xffbinary"
    manifest = build_vector_rebuild_manifest(
        catalog_revision=_digest(b"catalog"),
        collection={
            "id": "collection-kb",
            "space_id": "space-kb",
            "version": "v1",
            "capabilities": ["document_rag_query"],
            "asset_ids": ["asset-a"],
        },
        assets=[{"id": "asset-a", "revision": "rev-a", "content_digest": _digest(source)}],
    )

    class _Page:
        def extract_text(self):
            return "PDF page text"

    class _Reader:
        def __init__(self, _stream):
            self.pages = [_Page()]

    monkeypatch.setitem(sys.modules, "pypdf", types.SimpleNamespace(PdfReader=_Reader))
    chunks = build_text_chunks(manifest=manifest, source_bytes={"asset-a": source})
    assert chunks[0].text == "PDF page text"
    assert chunks[0].content_digest == _digest(source)


def test_embedded_rows_validate_bounded_batches_and_preserve_stable_identity() -> None:
    manifest, source = _manifest()
    chunks = build_text_chunks(manifest=manifest, source_bytes={"asset-a": source}, max_chars=200)
    calls = []

    def embed(texts):
        calls.append(len(texts))
        return [[0.1, 0.2] for _ in texts]

    rows = build_embedded_rows(chunks=chunks, embed=embed, dimension=2, batch_size=3)
    assert calls == [3] * (len(chunks) // 3) + ([len(chunks) % 3] if len(chunks) % 3 else [])
    assert rows[0].to_dict()["id"] == rows[0].to_dict()["doc_id"] + ":chunk_1"

    with pytest.raises(VectorRebuildPlanError, match="count"):
        build_embedded_rows(chunks=chunks, embed=lambda _: [], dimension=2)
    with pytest.raises(VectorRebuildPlanError, match="dimension"):
        build_embedded_rows(chunks=chunks, embed=lambda texts: [[0.1] for _ in texts], dimension=2)
