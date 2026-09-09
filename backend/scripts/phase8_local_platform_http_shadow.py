"""Replay the local Platform Query edge through an isolated HTTP sidecar app.

The command copies the staged Catalog, materializes only the explicitly
provided published Wiki root in that temporary copy, and calls the real
FastAPI edge through TestClient.  It never starts production traffic and never
persists raw HTTP bodies, Wiki excerpts, physical paths, or credentials.
"""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from knowledge_contracts import Principal
from knowledge_platform.catalog import SqliteCatalogQueryRepository
from knowledge_platform.local.app import _build_app as _build_app
from scripts.phase6_local_wiki_query_shadow import _materialize_catalog as _materialize_catalog
from scripts.phase6_local_wiki_query_shadow import _query_summary

_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_OUTPUT_DIR = _ROOT / "artifacts/phase0b-local-catalog"
_DEFAULT_CATALOG = _DEFAULT_OUTPUT_DIR / "knowledge-platform.sqlite3"
_SPACE_ID = "space_kb_default"
_COLLECTION_ID = "dataset_kb_default"


def _http_summary(response: Any) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        return {"http_status": response.status_code, "status": "invalid_json"}
    summary = _query_summary(payload)
    summary["http_status"] = response.status_code
    return summary


def _collection_summary(response: Any) -> dict[str, Any]:
    """Summarize Collection discovery without persisting names or bodies."""
    try:
        payload = response.json()
    except ValueError:
        return {"http_status": response.status_code, "status": "invalid_json"}
    if not isinstance(payload, dict):
        return {"http_status": response.status_code, "status": "invalid"}
    data = payload.get("data")
    collections = data.get("collections") if isinstance(data, dict) else None
    if not isinstance(collections, list):
        return {"http_status": response.status_code, "status": payload.get("status"), "collection_count": 0}
    return {
        "http_status": response.status_code,
        "status": payload.get("status"),
        "collection_count": len(collections),
    }


def _mcp_collection_summary(response: Any, *, collection_uri: str) -> dict[str, Any]:
    """Summarize MCP Collection list/read without persisting resource content."""
    try:
        payload = response.json()
    except ValueError:
        return {"http_status": response.status_code, "status": "invalid_json"}
    result = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(result, dict):
        return {"http_status": response.status_code, "status": "invalid"}
    resources = result.get("resources")
    if isinstance(resources, list):
        return {
            "http_status": response.status_code,
            "status": "ok",
            "collection_uri_advertised": any(
                isinstance(item, dict) and item.get("uri") == collection_uri for item in resources
            ),
            "collection_uri_count": sum(
                isinstance(item, dict) and "/collections/" in str(item.get("uri") or "") for item in resources
            ),
        }
    structured = result.get("structuredContent")
    return {
        "http_status": response.status_code,
        "status": structured.get("status") if isinstance(structured, dict) else "invalid",
        "canonical_uri_read": any(
            isinstance(item, dict) and item.get("uri") == collection_uri for item in (result.get("contents") or [])
        ),
    }




def run_shadow(
    *,
    catalog: Path = _DEFAULT_CATALOG,
    wiki_root: Path,
    query: str,
    output_dir: Path = _DEFAULT_OUTPUT_DIR,
    limit: int = 5,
) -> dict[str, Any]:
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {
        "format": "agent-knowledge-platform-phase8-local-platform-http-shadow/v1",
        "activation": "not-activated",
        "status": "PHASE8_LOCAL_PLATFORM_HTTP_SHADOW_FAILED",
        "materialized": None,
        "http": None,
    }
    try:
        catalog = catalog.expanduser().absolute()
        with tempfile.TemporaryDirectory(prefix="phase8-local-platform-http-shadow-") as temp_dir:
            temporary_catalog = Path(temp_dir) / "knowledge-platform.sqlite3"
            materialized = _materialize_catalog(catalog, temporary_catalog, wiki_root)
            repository = SqliteCatalogQueryRepository(temporary_catalog)
            bindings = materialized["file_bindings"]
            allowed = Principal(
                subject_id="phase8-local-platform-http-shadow",
                scopes=("knowledge.list", "knowledge.query", "knowledge.search", "knowledge.processing", f"knowledge.space:{_SPACE_ID}"),
            )
            denied = Principal(subject_id="phase8-local-platform-http-shadow-denied")
            app = _build_app(repository, bindings, allowed)
            denied_app = _build_app(repository, bindings, denied)
            with TestClient(app) as client, TestClient(denied_app) as denied_client:
                collections_response = client.get("/v1/collections", params={"space_id": _SPACE_ID})
                legacy_dataset_response = client.get("/v1/datasets", params={"space_id": _SPACE_ID})
                wiki_response = client.post(
                    "/v1/wiki/query",
                    json={"query": query, "space_id": _SPACE_ID, "limit": limit},
                )
                routed_response = client.post(
                    "/v1/knowledge/query",
                    json={
                        "query": query,
                        "space_id": _SPACE_ID,
                        "collection_id": _COLLECTION_ID,
                        "capability_hint": "wiki_query",
                        "limit": limit,
                    },
                )
                denied_response = denied_client.post(
                    "/v1/knowledge/query",
                    json={"query": query, "space_id": _SPACE_ID, "limit": limit},
                )
                mcp_response = client.post(
                    "/mcp",
                    json={
                        "jsonrpc": "2.0",
                        "id": "mcp-1",
                        "method": "tools/call",
                        "params": {
                            "name": "knowledge_query",
                            "arguments": {
                                "query": query,
                                "space_id": _SPACE_ID,
                                "collection_id": _COLLECTION_ID,
                                "capability_hint": "wiki_query",
                                "limit": limit,
                            },
                        },
                    },
                )
                collection_uri = f"knowledge://spaces/{_SPACE_ID}/collections/{_COLLECTION_ID}"
                mcp_resources_response = client.post(
                    "/mcp",
                    json={"jsonrpc": "2.0", "id": "mcp-resources", "method": "resources/list", "params": {}},
                )
                mcp_collection_read_response = client.post(
                    "/mcp",
                    json={
                        "jsonrpc": "2.0",
                        "id": "mcp-collection-read",
                        "method": "resources/read",
                        "params": {"uri": collection_uri},
                    },
                )
            result["materialized"] = {
                "pages": materialized["pages"],
                "asset_ids": materialized["asset_ids"],
                "page_digests": materialized["page_digests"],
            }
            result["http"] = {
                "collections": _collection_summary(collections_response),
                "legacy_dataset_alias": _collection_summary(legacy_dataset_response),
                "wiki_query": _http_summary(wiki_response),
                "knowledge_query": _http_summary(routed_response),
                "denied_knowledge_query": _http_summary(denied_response),
                "mcp_knowledge_query": _mcp_summary(mcp_response),
                "mcp_collection_resources": _mcp_collection_summary(
                    mcp_resources_response, collection_uri=collection_uri
                ),
                "mcp_collection_read": _mcp_collection_summary(
                    mcp_collection_read_response, collection_uri=collection_uri
                ),
            }
            if (
                result["http"]["collections"].get("status") == "ok"
                and result["http"]["collections"].get("collection_count", 0) > 0
                and result["http"]["legacy_dataset_alias"].get("status") == "ok"
                and result["http"]["wiki_query"].get("status") == "ok"
                and result["http"]["knowledge_query"].get("status") == "ok"
                and result["http"]["denied_knowledge_query"].get("error", {}).get("code") == "permission_denied"
                and result["http"]["mcp_knowledge_query"].get("status") == "ok"
                and result["http"]["mcp_collection_resources"].get("collection_uri_advertised") is True
                and result["http"]["mcp_collection_read"].get("canonical_uri_read") is True
                and result["http"]["mcp_collection_read"].get("status") == "ok"
            ):
                result["status"] = "PHASE8_LOCAL_PLATFORM_HTTP_SHADOW_PASS_NOT_ACTIVATABLE"
    except Exception as error:  # reports must not expose lower-layer details
        result["error_type"] = type(error).__name__
    return _write_report(output_dir, result)


def _mcp_summary(response: Any) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        return {"http_status": response.status_code, "status": "invalid_json"}
    if not isinstance(payload, dict):
        return {"http_status": response.status_code, "status": "invalid"}
    error = payload.get("error")
    if isinstance(error, dict):
        return {"http_status": response.status_code, "status": "error", "error": {"code": error.get("code")}}
    result = payload.get("result")
    if not isinstance(result, dict):
        return {"http_status": response.status_code, "status": "invalid"}
    structured = result.get("structuredContent")
    summary = _query_summary(structured if isinstance(structured, dict) else {})
    summary["http_status"] = response.status_code
    return summary


def _write_report(output_dir: Path, result: dict[str, Any]) -> dict[str, Any]:
    report_path = output_dir / "phase8-local-platform-http-shadow-report.json"
    report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    result["report"] = str(report_path)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=_DEFAULT_CATALOG)
    parser.add_argument("--wiki-root", type=Path, required=True)
    parser.add_argument("--query", required=True)
    parser.add_argument("--output-dir", type=Path, default=_DEFAULT_OUTPUT_DIR)
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args()
    result = run_shadow(
        catalog=args.catalog,
        wiki_root=args.wiki_root,
        query=args.query,
        output_dir=args.output_dir,
        limit=args.limit,
    )
    print(json.dumps({"status": result["status"], "report": result["report"]}, ensure_ascii=False))
    return 0 if str(result["status"]).endswith("NOT_ACTIVATABLE") else 1


if __name__ == "__main__":
    raise SystemExit(main())
