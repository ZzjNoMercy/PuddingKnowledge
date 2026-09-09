"""Audit whether a legacy local vector collection can be reused safely.

This is an observation-only check.  It compares stable chunk/document
identities and text digests, never emits text or vectors, and never creates,
updates, or drops a Milvus collection.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

_ROOT = Path(__file__).resolve().parents[2]
_OUTPUT_DIR = _ROOT / "artifacts/phase0b-local-catalog"
_MANIFEST = _OUTPUT_DIR / "phase8-local-vector-rebuild-manifest.json"
_CHUNKS = _OUTPUT_DIR / "phase8-local-vector-chunks.json"
_CATALOG = _OUTPUT_DIR / "knowledge-platform.sqlite3"
_LEGACY_COLLECTION = "puddingclaw_knowledge_text"


def _require_loopback(uri: str) -> str:
    parsed = urlparse(uri)
    if parsed.scheme not in {"http", "https", "grpc", "grpcs"} or parsed.hostname not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        raise ValueError("vector audit is limited to loopback")
    return uri


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _text_digest(value: object) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _embedding_dimension(description: dict[str, object]) -> int | None:
    for field in description.get("fields", []):
        if not isinstance(field, dict) or field.get("name") != "embedding":
            continue
        params = field.get("params")
        if isinstance(params, dict) and isinstance(params.get("dim"), int):
            return int(params["dim"])
    return None


def summarize_legacy_rows(
    *,
    chunks: list[dict[str, object]],
    rows: list[dict[str, object]],
    legacy_row_count: int,
    embedding_dimension: int | None,
) -> dict[str, object]:
    """Return a path-free compatibility summary without retaining row text."""

    current_ids = {str(item["chunk_id"]) for item in chunks}
    current_docs = {str(item["asset_id"]) for item in chunks}
    current_pairs = {(str(item["asset_id"]), _text_digest(item.get("text"))) for item in chunks}
    legacy_ids = {str(item.get("id")) for item in rows if item.get("id") is not None}
    legacy_docs = {str(item.get("doc_id")) for item in rows if item.get("doc_id") is not None}
    legacy_pairs = {
        (str(item.get("doc_id")), _text_digest(item.get("text")))
        for item in rows
        if item.get("doc_id") is not None
    }
    complete = legacy_row_count == len(rows)
    id_matches = len(current_ids & legacy_ids)
    pair_matches = len(current_pairs & legacy_pairs)
    compatible = (
        complete
        and len(chunks) > 0
        and legacy_row_count == len(chunks)
        and len(legacy_ids) == legacy_row_count
        and id_matches == len(current_ids)
        and pair_matches == len(current_pairs)
    )
    return {
        "legacy_row_count": legacy_row_count,
        "rows_observed": len(rows),
        "observation_complete": complete,
        "legacy_unique_ids": len(legacy_ids),
        "legacy_unique_doc_ids": len(legacy_docs),
        "current_chunk_count": len(chunks),
        "current_document_count": len(current_docs),
        "chunk_id_intersection": id_matches,
        "document_id_intersection": len(current_docs & legacy_docs),
        "document_id_text_digest_intersection": pair_matches,
        "embedding_dimension": embedding_dimension,
        "reuse_compatible": compatible,
        "reuse_allowed": False,
    }


def run_audit(
    *,
    manifest_path: Path = _MANIFEST,
    chunks_path: Path = _CHUNKS,
    catalog_path: Path = _CATALOG,
    vector_uri: str = "http://127.0.0.1:19530",
    vector_collection: str = _LEGACY_COLLECTION,
    max_rows: int = 10000,
    milvus_client: Any | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "format": "agent-knowledge-platform-phase8-local-dense-reuse-audit/v1",
        "status": "PHASE8_LOCAL_DENSE_REUSE_AUDIT_BLOCKED",
        "activation_allowed": False,
        "reuse_allowed": False,
        "network_contacted": False,
        "source_paths_emitted": False,
        "catalog_unchanged": None,
        "vector_collection": vector_collection,
    }
    before_digest = _file_digest(catalog_path) if catalog_path.is_file() else None
    client = milvus_client
    try:
        if max_rows < 1 or max_rows > 10000:
            raise ValueError("max_rows must be between 1 and 10000")
        _require_loopback(vector_uri)
        from scripts.phase8_local_dense_candidate import _load_inputs

        _, chunks = _load_inputs(manifest_path, chunks_path)
        if client is None:
            from pymilvus import MilvusClient

            client = MilvusClient(uri=vector_uri, timeout=10)
        description = client.describe_collection(collection_name=vector_collection)
        stats = client.get_collection_stats(collection_name=vector_collection)
        if not isinstance(stats, dict) or not isinstance(stats.get("row_count"), int):
            raise ValueError("legacy vector stats are invalid")
        legacy_row_count = int(stats["row_count"])
        if legacy_row_count > max_rows:
            raise ValueError("legacy vector collection exceeds bounded audit limit")
        rows = client.query(
            collection_name=vector_collection,
            filter="",
            output_fields=["id", "doc_id", "text"],
            limit=max(1, legacy_row_count),
        )
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError("legacy vector query returned an invalid shape")
        result.update(
            summarize_legacy_rows(
                chunks=[chunk.to_dict() for chunk in chunks],
                rows=rows,
                legacy_row_count=legacy_row_count,
                embedding_dimension=_embedding_dimension(description),
            )
        )
        result["status"] = (
            "PHASE8_LOCAL_DENSE_REUSE_AUDIT_INCOMPATIBLE_NOT_ACTIVATABLE"
            if not result["reuse_compatible"]
            else "PHASE8_LOCAL_DENSE_REUSE_AUDIT_COMPATIBLE_NOT_ACTIVATABLE"
        )
    except Exception as error:
        result["error_type"] = type(error).__name__
    finally:
        after_digest = _file_digest(catalog_path) if catalog_path.is_file() else None
        result["catalog_unchanged"] = before_digest == after_digest if before_digest is not None else False
        report_path = _OUTPUT_DIR / "phase8-local-dense-reuse-audit-report.json"
        report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        result["report"] = str(report_path)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=_MANIFEST)
    parser.add_argument("--chunks", type=Path, default=_CHUNKS)
    parser.add_argument("--catalog", type=Path, default=_CATALOG)
    parser.add_argument("--vector-uri", default="http://127.0.0.1:19530")
    parser.add_argument("--vector-collection", default=_LEGACY_COLLECTION)
    parser.add_argument("--max-rows", type=int, default=10000)
    args = parser.parse_args()
    result = run_audit(
        manifest_path=args.manifest,
        chunks_path=args.chunks,
        catalog_path=args.catalog,
        vector_uri=args.vector_uri,
        vector_collection=args.vector_collection,
        max_rows=args.max_rows,
    )
    print(json.dumps({"status": result["status"], "report": result["report"]}, ensure_ascii=False))
    return 0 if str(result["status"]).endswith("NOT_ACTIVATABLE") else 1


if __name__ == "__main__":
    raise SystemExit(main())
