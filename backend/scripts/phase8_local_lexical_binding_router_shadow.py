"""Bind the local lexical candidate in a Catalog copy and replay Query Plane routing."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from knowledge_contracts import Correlation, Principal
from knowledge_platform.catalog import (
    CatalogQueryService,
    SqliteCatalogQueryRepository,
    SqliteStructuredAssetWriter,
)
from knowledge_platform.catalog.deployment import (
    DeploymentActivationController,
    DeploymentArtifact,
    DeploymentManifest,
)
from knowledge_platform.catalog.deployment_sqlite import SqliteDeploymentActivationStore
from knowledge_platform.catalog.vector_rebuild import VectorRebuildManifest
from knowledge_platform.retrieval import (
    AssetReadService,
    CatalogSearchService,
    DocumentRetrievalService,
    LocalFilesystemBlobReader,
    MilvusBm25CatalogRetrievalProvider,
    WikiQueryService,
)
from knowledge_platform.router import KnowledgeQueryRouter, build_local_query_engines
from knowledge_platform.transport import McpQueryAdapter, RestQueryAdapter, create_platform_app

_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_OUTPUT_DIR = _ROOT / "artifacts/phase0b-local-catalog"
_DEFAULT_CATALOG = _DEFAULT_OUTPUT_DIR / "knowledge-platform.sqlite3"
_DEFAULT_MANIFEST = _DEFAULT_OUTPUT_DIR / "phase8-local-vector-lexical-manifest.json"
_DEFAULT_CHUNKS = _DEFAULT_OUTPUT_DIR / "phase8-local-vector-lexical-chunks.json"
_SPACE_ID = "space_kb_default"
_COLLECTION_ID = "dataset_kb_default"
_COLLECTION_VERSION = "legacy-73c066b66ad712df"
_PROVIDER_ID = "puddingclaw_platform_candidate_lexical_text"
_UNSAFE_QUERY_RE = re.compile(r"(?i)token|secret|password|credential|/Users/|file://")


def _digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _text_digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _deployment_manifest(*, revision: str, catalog_digest: str) -> DeploymentManifest:
    return DeploymentManifest(
        deployment_revision=revision,
        artifacts=(
            DeploymentArtifact("catalog", revision, "local-catalog", catalog_digest),
            DeploymentArtifact("blob", revision, "local-blob-tree", _text_digest("local-blob-tree")),
            DeploymentArtifact("vector_index", revision, _PROVIDER_ID, _text_digest(_PROVIDER_ID)),
            DeploymentArtifact("wiki_root", revision, "local-wiki-root", _text_digest("local-wiki-root")),
        ),
    )


def _summary(response: Any) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        return {"http_status": response.status_code, "status": "invalid_json"}
    if not isinstance(payload, dict):
        return {"http_status": response.status_code, "status": "invalid"}
    error = payload.get("error")
    if isinstance(error, dict):
        return {"http_status": response.status_code, "status": "error", "error": {"code": error.get("code")}}
    return {
        "http_status": response.status_code,
        "status": payload.get("status"),
        "count": payload.get("data", {}).get("count") if isinstance(payload.get("data"), dict) else None,
        "routing": payload.get("data", {}).get("routing") if isinstance(payload.get("data"), dict) else None,
    }


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
    structured = result.get("structuredContent") if isinstance(result, dict) else None
    if not isinstance(structured, dict):
        return {"http_status": response.status_code, "status": "invalid"}
    return {
        "http_status": response.status_code,
        "status": structured.get("status"),
        "count": structured.get("data", {}).get("count") if isinstance(structured.get("data"), dict) else None,
        "routing": structured.get("data", {}).get("routing") if isinstance(structured.get("data"), dict) else None,
    }


def _app(
    repository: SqliteCatalogQueryRepository,
    provider: MilvusBm25CatalogRetrievalProvider,
    principal: Principal,
    deployment: DeploymentActivationController,
):
    catalog = CatalogQueryService(repository)
    document = DocumentRetrievalService(provider, repository)
    wiki = WikiQueryService(provider, repository)
    rest = RestQueryAdapter(
        catalog=catalog,
        search=CatalogSearchService(catalog),
        asset_read=AssetReadService(catalog=repository, reader=LocalFilesystemBlobReader({})),
        document=document,
        wiki=wiki,
        knowledge_query=KnowledgeQueryRouter(
            catalog=repository,
            engines=build_local_query_engines(
                document=document,
                document_provider_id=_PROVIDER_ID,
            ),
        ),
        deployment=deployment,
    )
    return create_platform_app(
        query_adapter=rest,
        mcp_adapter=McpQueryAdapter(rest),
        principal_provider=lambda: principal,
        correlation_provider=lambda: Correlation("phase8-local-lexical-binding-shadow"),
    )


def run_shadow(
    *,
    catalog: Path = _DEFAULT_CATALOG,
    manifest: Path = _DEFAULT_MANIFEST,
    chunks: Path = _DEFAULT_CHUNKS,
    vector_uri: str = "http://127.0.0.1:19530",
    query: str | None = None,
    output_dir: Path = _DEFAULT_OUTPUT_DIR,
) -> dict[str, Any]:
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {
        "format": "agent-knowledge-platform-phase8-local-lexical-binding-router-shadow/v1",
        "activation": "not-activated",
        "status": "PHASE8_LOCAL_LEXICAL_BINDING_ROUTER_SHADOW_FAILED",
        "canonical_catalog_unchanged": False,
        "catalog_binding": None,
        "vector_identity": None,
        "query_plane": None,
        "source_paths_emitted": False,
        "network_contacted": False,
        "production_endpoint": False,
    }
    catalog = catalog.expanduser().absolute()
    canonical_before = _digest(catalog)
    try:
        raw_manifest = json.loads(manifest.expanduser().read_text(encoding="utf-8"))
        vector_manifest = VectorRebuildManifest.from_dict(raw_manifest)
        if vector_manifest.provider_collection_name != _PROVIDER_ID:
            raise ValueError("manifest provider does not match lexical candidate")
        from pymilvus import MilvusClient

        client = MilvusClient(uri=vector_uri, timeout=3)
        rows = client.query(
            collection_name=_PROVIDER_ID,
            filter="",
            output_fields=["doc_id"],
            limit=1000,
        )
        vector_ids = {
            str(row["doc_id"])
            for row in rows
            if isinstance(row, dict) and isinstance(row.get("doc_id"), str)
        }
        with tempfile.TemporaryDirectory(prefix="phase8-lexical-binding-router-") as temp_dir:
            temporary_catalog = Path(temp_dir) / "knowledge-platform.sqlite3"
            shutil.copy2(catalog, temporary_catalog)
            legacy_manifest = _deployment_manifest(
                revision="legacy-local-phase8-v1",
                catalog_digest=_digest(temporary_catalog),
            )
            candidate_manifest = _deployment_manifest(
                revision="platform-local-phase8-v1",
                catalog_digest=_digest(temporary_catalog),
            )
            deployment = DeploymentActivationController(
                store=SqliteDeploymentActivationStore(Path(temp_dir) / "deployment.sqlite3")
            )
            deployment.prepare(
                installation_id="phase8-local-binding",
                legacy_manifest=legacy_manifest,
                candidate_manifest=candidate_manifest,
            )
            deployment.mark_drained(proof_digest=_text_digest("phase8-local-binding-drain"))
            deployment.verify(
                legacy_before_digest=legacy_manifest.manifest_digest(),
                legacy_after_digest=legacy_manifest.manifest_digest(),
                candidate_manifest_digest=candidate_manifest.manifest_digest(),
                checks={"legacy_unchanged": True, "candidate_manifest_matches": True, "bundle_integrity": True},
            )
            deployment.activate(deployment_revision=candidate_manifest.deployment_revision)
            writer = SqliteStructuredAssetWriter(temporary_catalog)
            writer.bind_collection_provider(
                principal=Principal(
                    subject_id="phase8-local-binding-admin",
                    scopes=("knowledge.processing", f"knowledge.space:{_SPACE_ID}"),
                ),
                collection_id=_COLLECTION_ID,
                collection_version=_COLLECTION_VERSION,
                space_id=_SPACE_ID,
                capability="document_rag_query",
                binding={"provider_id": _PROVIDER_ID},
            )
            repository = SqliteCatalogQueryRepository(temporary_catalog)
            collection = next(
                item for item in repository.list_collections(space_id=_SPACE_ID) if item.get("id") == _COLLECTION_ID
            )
            binding = collection.get("provider_bindings", {}).get("document_rag_query")
            provider = MilvusBm25CatalogRetrievalProvider(
                catalog=repository,
                client=client,
                collection_name=_PROVIDER_ID,
                manifest=vector_manifest,
            )
            if query is None:
                raw_chunks = json.loads(chunks.expanduser().read_text(encoding="utf-8"))
                chunk_rows = raw_chunks.get("chunks") if isinstance(raw_chunks, dict) else None
                if not isinstance(chunk_rows, list):
                    raise ValueError("lexical candidate chunks are unavailable")
                for row in chunk_rows:
                    if not isinstance(row, dict) or not isinstance(row.get("text"), str):
                        continue
                    candidate_query = row["text"][:120]
                    if not candidate_query.strip() or _UNSAFE_QUERY_RE.search(candidate_query):
                        continue
                    try:
                        candidates = asyncio.run(
                            provider.search(query=candidate_query, space_id=_SPACE_ID, limit=1)
                        )
                    except Exception:
                        continue
                    if candidates and all(not _UNSAFE_QUERY_RE.search(item.quote) for item in candidates):
                        query = candidate_query
                        break
            if query is None or not query.strip():
                raise ValueError("lexical candidate query is empty")
            allowed = Principal(
                subject_id="phase8-local-lexical-binding-query",
                scopes=("knowledge.query", "knowledge.search", f"knowledge.space:{_SPACE_ID}"),
            )
            denied = Principal(subject_id="phase8-local-lexical-binding-denied")
            app = _app(repository, provider, allowed, deployment)
            denied_app = _app(repository, provider, denied, deployment)
            body = {
                "query": query,
                "space_id": _SPACE_ID,
                "collection_id": _COLLECTION_ID,
                "capability_hint": "document_rag_query",
                "limit": 1,
            }
            with TestClient(app) as http, TestClient(denied_app) as denied_http:
                rest_response = http.post("/v1/knowledge/query", json=body)
                mcp_response = http.post(
                    "/mcp",
                    json={
                        "jsonrpc": "2.0",
                        "id": "phase8-lexical-binding-mcp",
                        "method": "tools/call",
                        "params": {"name": "knowledge_query", "arguments": body},
                    },
                )
                denied_response = denied_http.post("/v1/knowledge/query", json=body)
            result["catalog_binding"] = {
                "provider_binding_present": binding == {"provider_id": _PROVIDER_ID},
                "asset_count": len(collection.get("asset_ids", [])),
            }
            result["vector_identity"] = {
                "manifest_document_count": len(vector_manifest.documents),
                "vector_identity_count": len(vector_ids),
                "manifest_identity_coverage": len(vector_ids & {item.asset_id for item in vector_manifest.documents})
                / len(vector_ids)
                if vector_ids
                else 0.0,
            }
            result["query_plane"] = {
                "rest": _summary(rest_response),
                "mcp": _mcp_summary(mcp_response),
                "denied_rest": _summary(denied_response),
            }
            active_context = deployment.capture_read_context()
            deployment.rollback(reason="controlled local lexical binding rollback")
            result["deployment_pointer"] = {
                "query_revision": active_context.deployment_revision,
                "query_manifest_digest": active_context.manifest_digest,
                "rollback_status": deployment.state.status,
                "rollback_active_revision": deployment.state.active_deployment_revision,
            }
        result["canonical_catalog_unchanged"] = canonical_before == _digest(catalog)
        if (
            result["catalog_binding"]["provider_binding_present"]
            and result["vector_identity"]["manifest_identity_coverage"] == 1.0
            and result["query_plane"]["rest"]["status"] == "ok"
            and result["query_plane"]["mcp"]["status"] == "ok"
            and result["query_plane"]["denied_rest"].get("error", {}).get("code") == "permission_denied"
            and result["deployment_pointer"]["query_revision"] == "platform-local-phase8-v1"
            and result["deployment_pointer"]["rollback_status"] == "rolled_back"
            and result["deployment_pointer"]["rollback_active_revision"] == "legacy-local-phase8-v1"
            and result["canonical_catalog_unchanged"]
        ):
            result["status"] = "PHASE8_LOCAL_LEXICAL_BINDING_ROUTER_SHADOW_PASS_NOT_ACTIVATABLE"
    except Exception as error:  # reports contain only exception type
        result["error_type"] = type(error).__name__
        result["canonical_catalog_unchanged"] = canonical_before == _digest(catalog)
    report_path = output_dir / "phase8-local-lexical-binding-router-shadow-report.json"
    report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    result["report"] = str(report_path)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=_DEFAULT_CATALOG)
    parser.add_argument("--manifest", type=Path, default=_DEFAULT_MANIFEST)
    parser.add_argument("--vector-uri", default="http://127.0.0.1:19530")
    parser.add_argument("--chunks", type=Path, default=_DEFAULT_CHUNKS)
    parser.add_argument("--query")
    parser.add_argument("--output-dir", type=Path, default=_DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    result = run_shadow(**vars(args))
    print(json.dumps({"status": result["status"], "report": result["report"]}, ensure_ascii=False))
    return 0 if str(result["status"]).endswith("NOT_ACTIVATABLE") else 1


if __name__ == "__main__":
    raise SystemExit(main())
