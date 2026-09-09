"""Replay the real dense candidate through the Platform REST and MCP edge."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from pymilvus import MilvusClient

from knowledge_contracts import Correlation, Principal
from knowledge_platform.catalog import CatalogQueryService, SqliteCatalogQueryRepository
from knowledge_platform.retrieval import (
    AssetReadService,
    CatalogSearchService,
    DocumentRetrievalService,
    LocalFilesystemBlobReader,
    WikiQueryService,
)
from knowledge_platform.retrieval.local_transformers_embedding import LocalTransformersEmbeddingClient
from knowledge_platform.retrieval.milvus import MilvusCatalogRetrievalProvider
from knowledge_platform.router import KnowledgeQueryRouter, build_local_query_engines
from knowledge_platform.transport import McpQueryAdapter, RestQueryAdapter, create_platform_app
from scripts.phase8_local_dense_candidate import _load_inputs
from scripts.phase8_local_vector_rebuild_manifest import _DEFAULT_SOURCE_ROOTS, _source_digest_index

_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_OUTPUT_DIR = _ROOT / "artifacts/phase0b-local-catalog"
_DEFAULT_CATALOG = _DEFAULT_OUTPUT_DIR / "knowledge-platform.sqlite3"
_DEFAULT_MANIFEST = _DEFAULT_OUTPUT_DIR / "phase8-local-vector-rebuild-manifest.json"
_DEFAULT_CHUNKS = _DEFAULT_OUTPUT_DIR / "phase8-local-vector-chunks.json"
_DEFAULT_MODEL_DIR = Path("/Users/pet/models/jina-embeddings-v4-vllm-retrieval")
_SPACE_ID = "space_kb_default"
_COLLECTION_ID = "dataset_kb_default"
_CANDIDATE = "puddingclaw_platform_candidate_text"


def _safe_output_dir(path: Path) -> Path:
    absolute = path.expanduser().absolute()
    current = absolute
    while current != current.parent:
        if current.is_symlink():
            raise ValueError("dense HTTP output directory must not contain symlink components")
        current = current.parent
    absolute.mkdir(parents=True, exist_ok=True)
    return absolute.resolve()


def _summary(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {"status": "invalid"}
    error = payload.get("error")
    evidence = payload.get("evidence")
    summary: dict[str, Any] = {
        "status": payload.get("status"),
        "evidence_count": len(evidence) if isinstance(evidence, list) else 0,
    }
    if isinstance(error, dict):
        summary["error_code"] = error.get("code")
    if isinstance(evidence, list):
        summary["all_evidence_uri_bound"] = all(
            isinstance(item, dict) and str(item.get("resource_uri") or "").startswith("knowledge://")
            for item in evidence
        )
    return summary


def _mcp_summary(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {"status": "invalid"}
    if isinstance(payload.get("error"), dict):
        return {"status": "error", "error_code": payload["error"].get("code")}
    result = payload.get("result")
    if not isinstance(result, dict):
        return {"status": "invalid"}
    return _summary(result.get("structuredContent"))


def _build_app(
    repository: SqliteCatalogQueryRepository,
    provider: Any,
    asset_paths: dict[str, Path],
    principal: Principal,
):
    catalog = CatalogQueryService(repository)
    document = DocumentRetrievalService(provider, repository)
    wiki = WikiQueryService(provider, repository)
    rest = RestQueryAdapter(
        catalog=catalog,
        search=CatalogSearchService(catalog),
        asset_read=AssetReadService(catalog=repository, reader=LocalFilesystemBlobReader(asset_paths)),
        document=document,
        wiki=wiki,
        knowledge_query=KnowledgeQueryRouter(
            catalog=repository,
            engines=build_local_query_engines(document=document, wiki=wiki),
        ),
    )
    return create_platform_app(
        query_adapter=rest,
        mcp_adapter=McpQueryAdapter(rest),
        principal_provider=lambda: principal,
        correlation_provider=lambda: Correlation("phase8-local-dense-platform-http-shadow"),
    )


def run_shadow(
    *,
    catalog: Path = _DEFAULT_CATALOG,
    manifest_path: Path = _DEFAULT_MANIFEST,
    chunks_path: Path = _DEFAULT_CHUNKS,
    model_dir: Path = _DEFAULT_MODEL_DIR,
    query: str = "knowledge platform",
    dimension: int = 2048,
    max_length: int = 1024,
    limit: int = 3,
    source_roots: tuple[Path, ...] = _DEFAULT_SOURCE_ROOTS,
    milvus_uri: str = "http://127.0.0.1:19530",
    output_dir: Path = _DEFAULT_OUTPUT_DIR,
    milvus_client: Any | None = None,
    catalog_repository: Any | None = None,
    embedding_client: Any | None = None,
) -> dict[str, Any]:
    output_dir = _safe_output_dir(output_dir)
    result: dict[str, Any] = {
        "format": "agent-knowledge-platform-phase8-local-dense-platform-http-shadow/v1",
        "status": "PHASE8_LOCAL_DENSE_PLATFORM_HTTP_SHADOW_BLOCKED",
        "activation_allowed": False,
        "candidate_collection_name": _CANDIDATE,
        "candidate_collection_created": False,
        "catalog_binding_written": False,
        "network_contacted": False,
        "external_network_contacted": False,
        "production_endpoint": False,
        "source_paths_emitted": False,
        "query_text_emitted": False,
        "vector_values_emitted": False,
        "embedding_status": "not_attempted",
    }
    provider_client = embedding_client
    owns_embedding_client = False
    try:
        if not isinstance(query, str) or not query.strip() or len(query) > 512:
            raise ValueError("dense HTTP query is invalid")
        if type(dimension) is not int or dimension < 1:
            raise ValueError("dense HTTP dimension is invalid")
        if type(max_length) is not int or not 1 <= max_length <= 8192:
            raise ValueError("dense HTTP max_length is invalid")
        if type(limit) is not int or not 1 <= limit <= 10:
            raise ValueError("dense HTTP limit is invalid")
        manifest, chunks = _load_inputs(manifest_path, chunks_path)
        if manifest.provider_collection_name != _CANDIDATE:
            raise ValueError("dense HTTP manifest is not the candidate")
        repository = catalog_repository or SqliteCatalogQueryRepository(catalog)
        revision_before = repository.catalog_revision
        client = milvus_client or MilvusClient(uri=milvus_uri, timeout=10)
        if not client.has_collection(collection_name=_CANDIDATE):
            raise ValueError("dense candidate collection is unavailable")
        stats = client.get_collection_stats(collection_name=_CANDIDATE)
        row_count = int(stats.get("row_count", -1)) if isinstance(stats, dict) else -1
        if row_count != len(chunks):
            raise ValueError("dense candidate row count is not bound to chunks")
        provider_client = provider_client or LocalTransformersEmbeddingClient(
            model_dir=model_dir,
            dimension=dimension,
            batch_size=1,
            max_length=max_length,
        )
        owns_embedding_client = embedding_client is None
        vector = provider_client.embed((query,))[0]
        provider = MilvusCatalogRetrievalProvider(
            catalog=repository,
            client=client,
            collection_name=_CANDIDATE,
            manifest=manifest,
            encode_query=lambda _query: vector,
        )
        source_index = _source_digest_index(source_roots)
        asset_paths = {
            document.asset_id: source_index[document.content_digest][0]
            for document in manifest.documents
            if source_index.get(document.content_digest)
        }
        allowed = Principal(
            subject_id="phase8-local-dense-platform-http-shadow",
            scopes=("knowledge.query", "knowledge.search", f"knowledge.space:{_SPACE_ID}"),
        )
        denied = Principal(subject_id="phase8-local-dense-platform-http-shadow-denied")
        app = _build_app(repository, provider, asset_paths, allowed)
        denied_app = _build_app(repository, provider, asset_paths, denied)
        with TestClient(app) as http, TestClient(denied_app) as denied_http:
            rest_response = http.post(
                "/v1/document-rag/query",
                json={"query": query, "space_id": _SPACE_ID, "limit": limit},
            )
            routed_response = http.post(
                "/v1/knowledge/query",
                json={
                    "query": query,
                    "space_id": _SPACE_ID,
                    "collection_id": _COLLECTION_ID,
                    "capability_hint": "document_rag_query",
                    "limit": limit,
                },
            )
            denied_response = denied_http.post(
                "/v1/document-rag/query",
                json={"query": query, "space_id": _SPACE_ID, "limit": limit},
            )
            mcp_response = http.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": "dense-http-1",
                    "method": "tools/call",
                    "params": {
                        "name": "document_rag_query",
                        "arguments": {"query": query, "space_id": _SPACE_ID, "limit": limit},
                    },
                },
            )
        revision_after = repository.catalog_revision
        payloads = {
            "rest_document": rest_response.json(),
            "routed_knowledge": routed_response.json(),
            "denied_document": denied_response.json(),
            "mcp_document": mcp_response.json(),
        }
        serialized = json.dumps(payloads, ensure_ascii=False)
        if any(marker in serialized for marker in ("source_path", "/Users/", "file://")):
            raise RuntimeError("HTTP response exposed a physical source path")
        summaries = {
            "rest_document": _summary(payloads["rest_document"]),
            "routed_knowledge": _summary(payloads["routed_knowledge"]),
            "denied_document": _summary(payloads["denied_document"]),
            "mcp_document": _mcp_summary(payloads["mcp_document"]),
        }
        result.update(
            {
                "manifest_digest": manifest.manifest_digest(),
                "embedding_dimension": dimension,
                "embedding_provider_version": getattr(provider_client, "provider_version", None),
                "embedding_transport": "in_process",
                "embedding_status": "verified",
                "candidate_row_count": row_count,
                "catalog_revision_unchanged": revision_before == revision_after,
                "query_length": len(query),
                "limit": limit,
                "http": summaries,
            }
        )
        if (
            summaries["rest_document"].get("status") == "ok"
            and summaries["rest_document"].get("evidence_count", 0) >= 1
            and summaries["rest_document"].get("all_evidence_uri_bound") is True
            and summaries["routed_knowledge"].get("status") == "ok"
            and summaries["mcp_document"].get("status") == "ok"
            and summaries["denied_document"].get("error_code") == "permission_denied"
            and result["catalog_revision_unchanged"]
        ):
            result["status"] = "PHASE8_LOCAL_DENSE_PLATFORM_HTTP_SHADOW_PASS_NOT_ACTIVATABLE"
    except Exception as error:
        result["error_type"] = type(error).__name__
    finally:
        close = getattr(provider_client, "close", None)
        if owns_embedding_client and callable(close):
            close()
        report_path = output_dir / "phase8-local-dense-platform-http-shadow-report.json"
        report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        result["report"] = str(report_path)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=_DEFAULT_CATALOG)
    parser.add_argument("--manifest", dest="manifest_path", type=Path, default=_DEFAULT_MANIFEST)
    parser.add_argument("--chunks", dest="chunks_path", type=Path, default=_DEFAULT_CHUNKS)
    parser.add_argument("--model-dir", type=Path, default=_DEFAULT_MODEL_DIR)
    parser.add_argument("--query", default="knowledge platform")
    parser.add_argument("--dimension", type=int, default=2048)
    parser.add_argument("--max-length", type=int, default=1024)
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--source-root", action="append", type=Path, dest="source_roots")
    parser.add_argument("--milvus-uri", default="http://127.0.0.1:19530")
    parser.add_argument("--output-dir", type=Path, default=_DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    roots = tuple(args.source_roots) if args.source_roots else _DEFAULT_SOURCE_ROOTS
    values = vars(args)
    values["source_roots"] = roots
    result = run_shadow(**values)
    print(json.dumps({"status": result["status"], "report": result["report"]}, ensure_ascii=False))
    return 0 if str(result["status"]).endswith("NOT_ACTIVATABLE") else 1


if __name__ == "__main__":
    raise SystemExit(main())
