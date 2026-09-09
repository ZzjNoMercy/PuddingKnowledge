"""Audit local Catalog ↔ Milvus identity coverage without changing either side."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from knowledge_platform.catalog import SqliteCatalogQueryRepository
from knowledge_platform.catalog.vector_binding import audit_vector_catalog_binding

_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_OUTPUT_DIR = _ROOT / "artifacts/phase0b-local-catalog"
_DEFAULT_CATALOG = _DEFAULT_OUTPUT_DIR / "knowledge-platform.sqlite3"


def _read_vector_document_ids(*, vector_uri: str, collection: str, limit: int) -> list[str]:
    parsed = urlparse(vector_uri)
    if parsed.scheme not in {"http", "https", "grpc", "grpcs"} or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("vector audit is limited to loopback")
    from pymilvus import MilvusClient

    client = MilvusClient(uri=vector_uri, timeout=2)
    rows = client.query(
        collection_name=collection,
        filter="",
        output_fields=["doc_id"],
        limit=limit,
    )
    if not isinstance(rows, list):
        raise ValueError("vector query returned an invalid shape")
    return [str(row["doc_id"]) for row in rows if isinstance(row, dict) and isinstance(row.get("doc_id"), str)]


def run_audit(
    *,
    catalog: Path = _DEFAULT_CATALOG,
    vector_uri: str = "http://127.0.0.1:19530",
    vector_collection: str = "puddingclaw_knowledge_text",
    space_id: str = "space_kb_default",
    collection_id: str = "dataset_kb_default",
    collection_version: str = "legacy-73c066b66ad712df",
    capability: str = "document_rag_query",
    sample_limit: int = 100,
    output_dir: Path = _DEFAULT_OUTPUT_DIR,
) -> dict[str, Any]:
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {
        "format": "agent-knowledge-platform-phase8-local-vector-binding-audit/v1",
        "status": "PHASE8_LOCAL_VECTOR_BINDING_AUDIT_BLOCKED",
        "activation_allowed": False,
        "vector_collection_observed": False,
        "catalog_binding_rows_observed": None,
        "sample_limit": sample_limit,
    }
    try:
        repository = SqliteCatalogQueryRepository(catalog)
        collection = next(
            (
                item
                for item in repository.list_collections(space_id=space_id)
                if item.get("id") == collection_id and item.get("version") == collection_version
            ),
            None,
        )
        result["catalog_binding_rows_observed"] = int(bool(collection and collection.get("provider_bindings", {}).get(capability)))
        vector_ids = _read_vector_document_ids(
            vector_uri=vector_uri,
            collection=vector_collection,
            limit=max(1, min(sample_limit, 1000)),
        )
        result["vector_collection_observed"] = True
        audit = audit_vector_catalog_binding(
            catalog=repository,
            space_id=space_id,
            collection_id=collection_id,
            collection_version=collection_version,
            capability=capability,
            vector_document_ids=vector_ids,
        )
        result.update(audit.to_dict())
        if audit.activation_allowed:
            result["status"] = "PHASE8_LOCAL_VECTOR_BINDING_AUDIT_PASS_NOT_ACTIVATABLE"
    except Exception as error:  # shadow must report unavailable providers, never activate
        result["error_type"] = type(error).__name__
    report_path = output_dir / "phase8-local-vector-binding-audit-report.json"
    report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    result["report"] = str(report_path)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=_DEFAULT_CATALOG)
    parser.add_argument("--vector-uri", default="http://127.0.0.1:19530")
    parser.add_argument("--vector-collection", default="puddingclaw_knowledge_text")
    parser.add_argument("--space-id", default="space_kb_default")
    parser.add_argument("--collection-id", default="dataset_kb_default")
    parser.add_argument("--collection-version", default="legacy-73c066b66ad712df")
    parser.add_argument("--capability", default="document_rag_query")
    parser.add_argument("--sample-limit", type=int, default=100)
    parser.add_argument("--output-dir", type=Path, default=_DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    result = run_audit(**vars(args))
    print(json.dumps({"status": result["status"], "report": result["report"]}, ensure_ascii=False))
    return 0 if str(result["status"]).endswith("NOT_ACTIVATABLE") else 1


if __name__ == "__main__":
    raise SystemExit(main())
