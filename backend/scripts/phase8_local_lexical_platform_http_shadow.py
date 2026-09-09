"""Replay the real lexical candidate through the Platform REST and MCP edge."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from pymilvus import MilvusClient

from knowledge_contracts import Correlation, Principal
from knowledge_platform.catalog import CatalogQueryService, SqliteCatalogQueryRepository
from knowledge_platform.catalog.vector_rebuild import VectorRebuildDocument, VectorRebuildManifest
from knowledge_platform.retrieval import (
    AssetReadService,
    CatalogSearchService,
    DocumentRetrievalService,
    LocalFilesystemBlobReader,
    WikiQueryService,
)
from knowledge_platform.retrieval.milvus import MilvusBm25CatalogRetrievalProvider
from knowledge_platform.router import KnowledgeQueryRouter, build_local_query_engines
from knowledge_platform.transport import McpQueryAdapter, RestQueryAdapter, create_platform_app
from scripts.phase8_local_vector_rebuild_manifest import _DEFAULT_SOURCE_ROOTS, _source_digest_index

_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_OUTPUT_DIR = _ROOT / "artifacts/phase0b-local-catalog"
_DEFAULT_CATALOG = _DEFAULT_OUTPUT_DIR / "knowledge-platform.sqlite3"
_DEFAULT_MANIFEST = _DEFAULT_OUTPUT_DIR / "phase8-local-vector-lexical-manifest.json"
_DEFAULT_CHUNKS = _DEFAULT_OUTPUT_DIR / "phase8-local-vector-lexical-chunks.json"
_SPACE_ID = "space_kb_default"
_COLLECTION_ID = "dataset_kb_default"


def _load_manifest(path: Path) -> VectorRebuildManifest:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("lexical manifest is invalid")
    raw_documents = payload.get("documents")
    if not isinstance(raw_documents, list):
        raise ValueError("lexical manifest documents are invalid")
    return VectorRebuildManifest(
        **{
            **payload,
            "documents": tuple(VectorRebuildDocument(**item) for item in raw_documents),
        }
    )


def _summary(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {"status": "invalid"}
    error = payload.get("error")
    evidence = payload.get("evidence")
    summary: dict[str, Any] = {"status": payload.get("status"), "evidence_count": len(evidence) if isinstance(evidence, list) else 0}
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
    structured = result.get("structuredContent")
    return _summary(structured)


def _build_app(repository: SqliteCatalogQueryRepository, provider: Any, asset_paths: dict[str, Path], principal: Principal):
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
        correlation_provider=lambda: Correlation("phase8-local-lexical-platform-http-shadow"),
    )


def run_shadow(
    *,
    catalog: Path = _DEFAULT_CATALOG,
    manifest_path: Path = _DEFAULT_MANIFEST,
    chunks_path: Path = _DEFAULT_CHUNKS,
    source_roots: tuple[Path, ...] = _DEFAULT_SOURCE_ROOTS,
    milvus_uri: str = "http://127.0.0.1:19530",
    output_dir: Path = _DEFAULT_OUTPUT_DIR,
) -> dict[str, Any]:
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {
        "format": "agent-knowledge-platform-phase8-local-lexical-platform-http-shadow/v1",
        "status": "PHASE8_LOCAL_LEXICAL_PLATFORM_HTTP_SHADOW_BLOCKED",
        "activation_allowed": False,
        "network_contacted": False,
        "production_endpoint": False,
        "source_paths_emitted": False,
    }
    try:
        manifest = _load_manifest(manifest_path)
        chunks_payload = json.loads(chunks_path.read_text(encoding="utf-8"))
        chunks = chunks_payload.get("chunks") if isinstance(chunks_payload, dict) else None
        if not isinstance(chunks, list) or not chunks or not isinstance(chunks[1].get("text"), str):
            raise ValueError("lexical chunks are invalid")
        query = chunks[1]["text"][:80]
        repository = SqliteCatalogQueryRepository(catalog)
        revision_before = repository.catalog_revision
        source_index = _source_digest_index(source_roots)
        asset_paths = {
            document.asset_id: source_index[document.content_digest][0]
            for document in manifest.documents
            if source_index.get(document.content_digest)
        }
        provider = MilvusBm25CatalogRetrievalProvider(
            catalog=repository,
            client=MilvusClient(uri=milvus_uri, timeout=10),
            collection_name=manifest.provider_collection_name,
            manifest=manifest,
        )
        allowed = Principal(
            subject_id="phase8-local-lexical-platform-http-shadow",
            scopes=("knowledge.query", "knowledge.search", f"knowledge.space:{_SPACE_ID}"),
        )
        denied = Principal(subject_id="phase8-local-lexical-platform-http-shadow-denied")
        app = _build_app(repository, provider, asset_paths, allowed)
        denied_app = _build_app(repository, provider, asset_paths, denied)
        with TestClient(app) as client, TestClient(denied_app) as denied_client:
            rest_response = client.post(
                "/v1/document-rag/query",
                json={"query": query, "space_id": _SPACE_ID, "limit": 1},
            )
            routed_response = client.post(
                "/v1/knowledge/query",
                json={
                    "query": query,
                    "space_id": _SPACE_ID,
                    "collection_id": _COLLECTION_ID,
                    "capability_hint": "document_rag_query",
                    "limit": 1,
                },
            )
            denied_response = denied_client.post(
                "/v1/document-rag/query",
                json={"query": query, "space_id": _SPACE_ID, "limit": 1},
            )
            mcp_response = client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": "lexical-http-1",
                    "method": "tools/call",
                    "params": {"name": "document_rag_query", "arguments": {"query": query, "space_id": _SPACE_ID, "limit": 1}},
                },
            )
        revision_after = repository.catalog_revision
        payloads = {
            "rest_document": rest_response.json(),
            "routed_knowledge": routed_response.json(),
            "denied_document": denied_response.json(),
            "mcp_document": mcp_response.json(),
        }
        summaries = {
            "rest_document": _summary(payloads["rest_document"]),
            "routed_knowledge": _summary(payloads["routed_knowledge"]),
            "denied_document": _summary(payloads["denied_document"]),
            "mcp_document": _mcp_summary(payloads["mcp_document"]),
        }
        serialized = json.dumps(payloads, ensure_ascii=False)
        if any(marker in serialized for marker in ("source_path", "/Users/", "file://")):
            raise RuntimeError("HTTP response exposed a physical source path")
        result.update(
            {
                "query_length": len(query),
                "manifest_digest": manifest.manifest_digest(),
                "catalog_revision_unchanged": revision_before == revision_after,
                "http": summaries,
            }
        )
        if (
            summaries["rest_document"].get("status") == "ok"
            and summaries["rest_document"].get("evidence_count") == 1
            and summaries["rest_document"].get("all_evidence_uri_bound") is True
            and summaries["routed_knowledge"].get("status") == "ok"
            and summaries["mcp_document"].get("status") == "ok"
            and summaries["denied_document"].get("error_code") == "permission_denied"
            and result["catalog_revision_unchanged"]
        ):
            result["status"] = "PHASE8_LOCAL_LEXICAL_PLATFORM_HTTP_SHADOW_PASS_NOT_ACTIVATABLE"
    except Exception as error:
        result["error_type"] = type(error).__name__
    report_path = output_dir / "phase8-local-lexical-platform-http-shadow-report.json"
    report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    result["report"] = str(report_path)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=_DEFAULT_CATALOG)
    parser.add_argument("--manifest", type=Path, default=_DEFAULT_MANIFEST)
    parser.add_argument("--chunks", type=Path, default=_DEFAULT_CHUNKS)
    parser.add_argument("--source-root", action="append", type=Path, dest="source_roots")
    parser.add_argument("--milvus-uri", default="http://127.0.0.1:19530")
    parser.add_argument("--output-dir", type=Path, default=_DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    roots = tuple(args.source_roots) if args.source_roots else _DEFAULT_SOURCE_ROOTS
    result = run_shadow(
        catalog=args.catalog,
        manifest_path=args.manifest,
        chunks_path=args.chunks,
        source_roots=roots,
        milvus_uri=args.milvus_uri,
        output_dir=args.output_dir,
    )
    print(json.dumps({"status": result["status"], "report": result["report"]}, ensure_ascii=False))
    return 0 if str(result["status"]).endswith("NOT_ACTIVATABLE") else 1


if __name__ == "__main__":
    raise SystemExit(main())
