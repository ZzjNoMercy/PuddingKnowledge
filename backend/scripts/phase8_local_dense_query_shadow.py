"""Query the local dense candidate through the Platform retrieval port.

This is a read-only shadow.  It embeds one bounded query with the explicitly
supplied local model, searches the candidate Milvus collection, resolves hits
through the staged Catalog, and records only bounded counts/digests.  It never
binds the candidate into Catalog state or changes activation.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

from pymilvus import MilvusClient

from knowledge_platform.catalog import SqliteCatalogQueryRepository
from knowledge_platform.catalog.vector_rebuild import VectorRebuildManifest
from knowledge_platform.retrieval.local_transformers_embedding import LocalTransformersEmbeddingClient
from knowledge_platform.retrieval.milvus import MilvusCatalogRetrievalProvider
from scripts.phase8_local_dense_candidate import _load_inputs

_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_OUTPUT_DIR = _ROOT / "artifacts/phase0b-local-catalog"
_DEFAULT_CATALOG = _DEFAULT_OUTPUT_DIR / "knowledge-platform.sqlite3"
_DEFAULT_MANIFEST = _DEFAULT_OUTPUT_DIR / "phase8-local-vector-rebuild-manifest.json"
_DEFAULT_CHUNKS = _DEFAULT_OUTPUT_DIR / "phase8-local-vector-chunks.json"
_DEFAULT_MODEL_DIR = Path("/Users/pet/models/jina-embeddings-v4-vllm-retrieval")
_COLLECTION = "puddingclaw_platform_candidate_text"
_SPACE_ID = "space_kb_default"


def _digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _safe_output_dir(path: Path) -> Path:
    absolute = path.expanduser().absolute()
    current = absolute
    while current != current.parent:
        if current.is_symlink():
            raise ValueError("dense query output directory must not contain symlink components")
        current = current.parent
    absolute.mkdir(parents=True, exist_ok=True)
    return absolute.resolve()


def _load_manifest(path: Path) -> VectorRebuildManifest:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("dense query manifest is invalid")
    return VectorRebuildManifest.from_dict(payload)


def run_shadow(
    *,
    catalog: Path = _DEFAULT_CATALOG,
    manifest_path: Path = _DEFAULT_MANIFEST,
    chunks_path: Path = _DEFAULT_CHUNKS,
    model_dir: Path = _DEFAULT_MODEL_DIR,
    query: str = "knowledge platform",
    dimension: int = 2048,
    max_length: int = 1024,
    milvus_uri: str = "http://127.0.0.1:19530",
    limit: int = 3,
    output_dir: Path = _DEFAULT_OUTPUT_DIR,
    milvus_client: Any | None = None,
    catalog_repository: Any | None = None,
    embedding_client: Any | None = None,
) -> dict[str, Any]:
    output_dir = _safe_output_dir(output_dir)
    result: dict[str, Any] = {
        "format": "agent-knowledge-platform-phase8-local-dense-query-shadow/v1",
        "status": "PHASE8_LOCAL_DENSE_QUERY_SHADOW_BLOCKED",
        "activation_allowed": False,
        "candidate_collection_name": _COLLECTION,
        "candidate_collection_created": False,
        "catalog_binding_written": False,
        "network_contacted": False,
        "external_network_contacted": False,
        "source_paths_emitted": False,
        "vector_values_emitted": False,
        "query_text_emitted": False,
        "embedding_status": "not_attempted",
    }
    provider_client = embedding_client
    owns_embedding_client = False
    try:
        if not isinstance(query, str) or not query.strip() or len(query) > 512:
            raise ValueError("dense query is invalid")
        if type(dimension) is not int or dimension < 1:
            raise ValueError("dense query dimension is invalid")
        if type(max_length) is not int or not 1 <= max_length <= 8192:
            raise ValueError("dense query max_length is invalid")
        if type(limit) is not int or not 1 <= limit <= 10:
            raise ValueError("dense query limit is invalid")
        manifest, chunks = _load_inputs(manifest_path, chunks_path)
        if manifest.provider_collection_name != _COLLECTION:
            raise ValueError("dense query manifest is not the candidate")
        repository = catalog_repository or SqliteCatalogQueryRepository(catalog)
        revision_before = repository.catalog_revision
        client = milvus_client or MilvusClient(uri=milvus_uri, timeout=10)
        if not client.has_collection(collection_name=_COLLECTION):
            raise ValueError("dense candidate collection is unavailable")
        stats = client.get_collection_stats(collection_name=_COLLECTION)
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
            collection_name=_COLLECTION,
            manifest=manifest,
            encode_query=lambda _query: vector,
        )
        candidates = asyncio.run(provider.search(query=query, space_id=_SPACE_ID, limit=limit))
        revision_after = repository.catalog_revision
        resource_uris = [candidate.resource_uri for candidate in candidates]
        result.update(
            {
                "manifest_digest": manifest.manifest_digest(),
                "embedding_dimension": dimension,
                "embedding_provider_version": getattr(provider_client, "provider_version", None),
                "embedding_transport": "in_process",
                "embedding_status": "verified",
                "candidate_row_count": row_count,
                "catalog_revision_unchanged": revision_before == revision_after,
                "result_count": len(candidates),
                "result_asset_ids_digest": _digest([candidate.asset_id for candidate in candidates]),
                "resource_uri_count": len(resource_uris),
                "resource_uri_knowledge_bound": all(uri.startswith("knowledge://") for uri in resource_uris),
                "scores_finite": all(isinstance(candidate.score, (int, float)) for candidate in candidates),
            }
        )
        if (
            candidates
            and result["catalog_revision_unchanged"]
            and result["resource_uri_knowledge_bound"]
            and result["scores_finite"]
        ):
            result["status"] = "PHASE8_LOCAL_DENSE_QUERY_SHADOW_PASS_NOT_ACTIVATABLE"
    except Exception as error:
        result["error_type"] = type(error).__name__
    finally:
        close = getattr(provider_client, "close", None)
        if owns_embedding_client and callable(close):
            close()
        report_path = output_dir / "phase8-local-dense-query-shadow-report.json"
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
    parser.add_argument("--milvus-uri", default="http://127.0.0.1:19530")
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--output-dir", type=Path, default=_DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    result = run_shadow(**vars(args))
    print(json.dumps({"status": result["status"], "result_count": result.get("result_count", 0), "report": result["report"]}, ensure_ascii=False))
    return 0 if str(result["status"]).endswith("NOT_ACTIVATABLE") else 1


if __name__ == "__main__":
    raise SystemExit(main())
