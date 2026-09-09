from __future__ import annotations

import hashlib
import json
from pathlib import Path

from scripts.phase8_local_embedding_cache_audit import run_audit


def _write_chunks(path: Path, texts: list[str]) -> None:
    path.write_text(json.dumps({"chunks": [{"text": text} for text in texts]}), encoding="utf-8")


def test_embedding_cache_audit_does_not_authorize_partial_or_unproven_cache(tmp_path: Path) -> None:
    chunks = tmp_path / "chunks.json"
    _write_chunks(chunks, ["alpha", "beta"])
    cache = tmp_path / "embedding-cache.jsonl"
    cache.write_text(
        json.dumps({hashlib.sha256(b"alpha").hexdigest(): [0.1, 0.2]}) + "\n",
        encoding="utf-8",
    )

    result = run_audit(cache_path=cache, chunks_path=chunks, output_dir=tmp_path / "reports")

    assert result["status"] == "PHASE8_LOCAL_DENSE_CACHE_NOT_REUSABLE"
    assert result["matching_chunk_count"] == 1
    assert result["current_chunk_count"] == 2
    assert result["reuse_allowed"] is False
    assert result["provider_metadata_present"] is False
    assert result["vector_values_emitted"] is False


def test_embedding_cache_audit_rejects_invalid_cache(tmp_path: Path) -> None:
    chunks = tmp_path / "chunks.json"
    _write_chunks(chunks, ["alpha"])
    cache = tmp_path / "embedding-cache.jsonl"
    cache.write_text(json.dumps({"not-a-digest": [0.1]}), encoding="utf-8")

    result = run_audit(cache_path=cache, chunks_path=chunks, output_dir=tmp_path / "reports")

    assert result["status"] == "PHASE8_LOCAL_DENSE_CACHE_NOT_REUSABLE"
    assert result["error_type"] == "ValueError"
    assert result["reuse_allowed"] is False


def test_embedding_cache_audit_full_coverage_still_requires_provenance(tmp_path: Path) -> None:
    chunks = tmp_path / "chunks.json"
    _write_chunks(chunks, ["alpha"])
    cache = tmp_path / "embedding-cache.jsonl"
    cache.write_text(json.dumps({hashlib.sha256(b"alpha").hexdigest(): [0.1, 0.2]}) + "\n", encoding="utf-8")

    result = run_audit(cache_path=cache, chunks_path=chunks, output_dir=tmp_path / "reports")

    assert result["matching_chunk_count"] == result["current_chunk_count"]
    assert result["reuse_blockers"] == ["cache_has_no_provider_model_version_provenance"]
    assert result["reuse_allowed"] is False


def test_embedding_cache_audit_rejects_symlink_inputs(tmp_path: Path) -> None:
    chunks = tmp_path / "chunks.json"
    _write_chunks(chunks, ["alpha"])
    cache_target = tmp_path / "embedding-cache-target.jsonl"
    cache_target.write_text(json.dumps({hashlib.sha256(b"alpha").hexdigest(): [0.1, 0.2]}), encoding="utf-8")
    cache_link = tmp_path / "embedding-cache.jsonl"
    cache_link.symlink_to(cache_target)

    result = run_audit(cache_path=cache_link, chunks_path=chunks, output_dir=tmp_path / "reports")

    assert result["status"] == "PHASE8_LOCAL_DENSE_CACHE_NOT_REUSABLE"
    assert result["error_type"] == "ValueError"
    assert result["reuse_allowed"] is False


def test_embedding_cache_audit_rejects_symlink_output_directory(tmp_path: Path) -> None:
    chunks = tmp_path / "chunks.json"
    _write_chunks(chunks, ["alpha"])
    cache = tmp_path / "embedding-cache.jsonl"
    cache.write_text(json.dumps({hashlib.sha256(b"alpha").hexdigest(): [0.1, 0.2]}), encoding="utf-8")
    output_target = tmp_path / "reports-target"
    output_target.mkdir()
    output_link = tmp_path / "reports-link"
    output_link.symlink_to(output_target, target_is_directory=True)

    try:
        run_audit(cache_path=cache, chunks_path=chunks, output_dir=output_link)
    except ValueError as error:
        assert str(error) == "embedding audit output directory must not be a symlink"
    else:
        raise AssertionError("symlink output directory was accepted")
