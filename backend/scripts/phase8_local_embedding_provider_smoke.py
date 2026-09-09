"""Run a bounded, real local embedding provider smoke without creating a Collection."""

from __future__ import annotations

import argparse
import json
import math
import time
from pathlib import Path
from typing import Any

from knowledge_platform.retrieval.local_transformers_embedding import LocalTransformersEmbeddingClient

_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_CHUNKS = _ROOT / "artifacts/phase0b-local-catalog/phase8-local-vector-chunks.json"
_DEFAULT_OUTPUT_DIR = _ROOT / "artifacts/phase0b-local-catalog"


def _read_regular_text(path: Path) -> str:
    path = path.expanduser()
    if path.is_symlink() or not path.is_file():
        raise ValueError("embedding smoke input must be a regular file")
    return path.read_text(encoding="utf-8")


def _prepare_output_dir(path: Path) -> Path:
    path = path.expanduser().absolute()
    current = path
    while current != current.parent:
        if current.is_symlink():
            raise ValueError("embedding smoke output directory must not be a symlink")
        current = current.parent
    if path.exists() and not path.is_dir():
        raise ValueError("embedding smoke output directory must be a directory")
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def _load_texts(path: Path, limit: int) -> tuple[int, tuple[str, ...]]:
    payload = json.loads(_read_regular_text(path))
    raw_chunks = payload.get("chunks") if isinstance(payload, dict) else None
    if not isinstance(raw_chunks, list) or not raw_chunks:
        raise ValueError("embedding smoke chunks are missing")
    texts = [item.get("text") for item in raw_chunks if isinstance(item, dict)]
    if len(texts) != len(raw_chunks) or any(not isinstance(text, str) or not text.strip() for text in texts):
        raise ValueError("embedding smoke chunk text is invalid")
    if len(set(texts)) != len(texts):
        raise ValueError("embedding smoke chunk text is not unique")
    return len(texts), tuple(texts[:limit])


def run_smoke(
    *,
    model_dir: Path,
    chunks_path: Path = _DEFAULT_CHUNKS,
    output_dir: Path = _DEFAULT_OUTPUT_DIR,
    dimension: int,
    limit: int = 2,
    batch_size: int = 2,
    max_length: int = 1024,
    embed_client: Any | None = None,
) -> dict[str, Any]:
    output_dir = _prepare_output_dir(output_dir)
    result: dict[str, Any] = {
        "format": "agent-knowledge-platform-phase8-local-embedding-provider-smoke/v1",
        "status": "PHASE8_LOCAL_DENSE_PROVIDER_SMOKE_FAILED",
        "activation_allowed": False,
        "network_contacted": False,
        "source_paths_emitted": False,
        "vector_values_emitted": False,
        "candidate_collection_created": False,
        "model_weights_loaded": False,
        "provider": "injected" if embed_client is not None else "local-transformers-v1",
    }
    client = embed_client
    try:
        if type(limit) is not int or not 1 <= limit <= 32:
            raise ValueError("embedding smoke limit is invalid")
        total_chunks, texts = _load_texts(chunks_path, limit)
        if len(texts) != limit:
            raise ValueError("embedding smoke limit exceeds available chunks")
        if client is None:
            client = LocalTransformersEmbeddingClient(
                model_dir=model_dir,
                dimension=dimension,
                batch_size=batch_size,
                max_length=max_length,
            )
            result["model_weights_loaded"] = True
        started = time.monotonic()
        vectors_list: list[tuple[float, ...]] = []
        for start in range(0, len(texts), batch_size):
            vectors_list.extend(client.embed(texts[start : start + batch_size]))
        vectors = tuple(vectors_list)
        elapsed = time.monotonic() - started
        if len(vectors) != len(texts) or any(len(vector) != dimension for vector in vectors):
            raise ValueError("embedding smoke vector shape is invalid")
        norms = [math.sqrt(sum(float(value) * float(value) for value in vector)) for vector in vectors]
        if any(not math.isfinite(value) for vector in vectors for value in vector) or any(not math.isfinite(value) for value in norms):
            raise ValueError("embedding smoke vector is non-finite")
        result.update(
            {
                "status": "PHASE8_LOCAL_DENSE_PROVIDER_SMOKE_PASS_NOT_ACTIVATABLE",
                "total_chunk_count": total_chunks,
                "smoke_chunk_count": len(texts),
                "embedding_dimension": dimension,
                "batch_size": batch_size,
                "max_length": max_length,
                "vector_count": len(vectors),
                "norm_min": round(min(norms), 6),
                "norm_max": round(max(norms), 6),
                "elapsed_seconds": round(elapsed, 3),
            }
        )
    except Exception as error:  # noqa: BLE001 - keep the smoke report bounded
        result["error_type"] = type(error).__name__
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()
    report_path = output_dir / "phase8-local-embedding-provider-smoke-report.json"
    if report_path.is_symlink():
        raise ValueError("embedding smoke report must not be a symlink")
    report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    result["report"] = str(report_path)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--chunks", type=Path, default=_DEFAULT_CHUNKS)
    parser.add_argument("--output-dir", type=Path, default=_DEFAULT_OUTPUT_DIR)
    parser.add_argument("--dimension", type=int, required=True)
    parser.add_argument("--limit", type=int, default=2)
    parser.add_argument("--batch-size", type=int, default=2)
    parser.add_argument("--max-length", type=int, default=1024)
    args = parser.parse_args()
    result = run_smoke(
        model_dir=args.model_dir,
        chunks_path=args.chunks,
        output_dir=args.output_dir,
        dimension=args.dimension,
        limit=args.limit,
        batch_size=args.batch_size,
        max_length=args.max_length,
    )
    print(json.dumps({"status": result["status"], "report": result["report"]}, ensure_ascii=False))
    return 0 if result["status"].endswith("PASS_NOT_ACTIVATABLE") else 1


if __name__ == "__main__":
    raise SystemExit(main())
