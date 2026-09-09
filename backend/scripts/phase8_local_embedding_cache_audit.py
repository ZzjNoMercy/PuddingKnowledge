"""Audit a local embedding cache for safe reuse by the current Vector chunks.

The cache is treated as an opaque local artifact.  This audit compares only
content hashes, vector shape and basic numeric validity; it never emits vector
values, source paths or cache contents, and it never authorizes collection
creation or activation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_CHUNKS = _ROOT / "artifacts/phase0b-local-catalog/phase8-local-vector-chunks.json"
_DEFAULT_OUTPUT_DIR = _ROOT / "artifacts/phase0b-local-catalog"
_DIGEST_LENGTH = 64


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read_regular_text(path: Path) -> str:
    path = path.expanduser()
    if path.is_symlink() or not path.is_file():
        raise ValueError("embedding audit input must be a regular file")
    return path.read_text(encoding="utf-8")


def _prepare_output_dir(path: Path) -> Path:
    path = path.expanduser().absolute()
    if path.is_symlink():
        raise ValueError("embedding audit output directory must not be a symlink")
    if path.exists() and not path.is_dir():
        raise ValueError("embedding audit output directory must be a directory")
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def _load_chunks(path: Path) -> tuple[str, ...]:
    payload = json.loads(_read_regular_text(path))
    raw_chunks = payload.get("chunks") if isinstance(payload, dict) else None
    if not isinstance(raw_chunks, list) or not raw_chunks:
        raise ValueError("vector chunks are missing")
    texts: list[str] = []
    for item in raw_chunks:
        if not isinstance(item, dict) or not isinstance(item.get("text"), str) or not item["text"]:
            raise ValueError("vector chunk text is invalid")
        texts.append(item["text"])
    if len(set(texts)) != len(texts):
        raise ValueError("vector chunk texts are not unique")
    return tuple(texts)


def _load_cache(path: Path) -> tuple[set[str], tuple[int, ...]]:
    keys: set[str] = set()
    dimensions: set[int] = set()
    for line_number, line in enumerate(_read_regular_text(path).splitlines(), start=1):
        payload = json.loads(line)
        if not isinstance(payload, dict) or len(payload) != 1:
            raise ValueError(f"embedding cache line {line_number} is invalid")
        key, vector = next(iter(payload.items()))
        if (
            not isinstance(key, str)
            or len(key) != _DIGEST_LENGTH
            or any(character not in "0123456789abcdef" for character in key)
            or not isinstance(vector, list)
            or not vector
            or any(not isinstance(value, (int, float)) or not math.isfinite(value) for value in vector)
        ):
            raise ValueError(f"embedding cache line {line_number} is invalid")
        if key in keys:
            raise ValueError("embedding cache contains duplicate keys")
        keys.add(key)
        dimensions.add(len(vector))
    if not keys:
        raise ValueError("embedding cache is empty")
    return keys, tuple(sorted(dimensions))


def run_audit(
    *,
    cache_path: Path,
    chunks_path: Path = _DEFAULT_CHUNKS,
    output_dir: Path = _DEFAULT_OUTPUT_DIR,
) -> dict[str, Any]:
    output_dir = _prepare_output_dir(output_dir)
    result: dict[str, Any] = {
        "format": "agent-knowledge-platform-phase8-local-embedding-cache-audit/v1",
        "status": "PHASE8_LOCAL_DENSE_CACHE_NOT_REUSABLE",
        "activation_allowed": False,
        "reuse_allowed": False,
        "network_contacted": False,
        "source_paths_emitted": False,
        "vector_values_emitted": False,
        "provider_metadata_present": False,
    }
    try:
        raw_output_dir = output_dir.expanduser().absolute()
        if raw_output_dir.is_symlink():
            raise ValueError("embedding audit output directory must not be a symlink")
        output_dir = raw_output_dir.resolve()
        texts = _load_chunks(chunks_path)
        keys, dimensions = _load_cache(cache_path)
        matching_count = sum(_digest(text) in keys for text in texts)
        result.update(
            {
                "current_chunk_count": len(texts),
                "cache_entry_count": len(keys),
                "cache_dimensions": list(dimensions),
                "matching_chunk_count": matching_count,
                "matching_chunk_ratio": matching_count / len(texts),
                "cache_format": "bare-jsonl-key-to-vector",
                "reuse_blockers": [
                    "cache_has_no_provider_model_version_provenance",
                    *(["cache_does_not_cover_current_chunks"] if matching_count != len(texts) else []),
                ],
            }
        )
    except Exception as error:
        result["error_type"] = type(error).__name__
    report_path = output_dir / "phase8-local-embedding-cache-audit-report.json"
    if report_path.is_symlink():
        raise ValueError("embedding audit report must not be a symlink")
    report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    result["report"] = str(report_path)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, required=True, help="explicit local JSONL embedding cache")
    parser.add_argument("--chunks", type=Path, default=_DEFAULT_CHUNKS)
    parser.add_argument("--output-dir", type=Path, default=_DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    result = run_audit(cache_path=args.cache, chunks_path=args.chunks, output_dir=args.output_dir)
    print(json.dumps({"status": result["status"], "report": result["report"]}, ensure_ascii=False))
    return 0 if result["status"] == "PHASE8_LOCAL_DENSE_CACHE_NOT_REUSABLE" and "error_type" not in result else 1


if __name__ == "__main__":
    raise SystemExit(main())
