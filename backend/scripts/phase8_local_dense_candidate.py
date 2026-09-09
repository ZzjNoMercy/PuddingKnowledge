"""Build a dense Milvus candidate from Catalog chunks with explicit network opt-in."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from knowledge_platform.catalog.vector_rebuild import (
    VectorEmbeddingRow,
    VectorRebuildChunk,
    VectorRebuildManifest,
    VectorRebuildPlanError,
    build_embedded_rows,
    build_embedded_rows_checkpointed,
)
from knowledge_platform.retrieval.embedding import OpenAICompatibleEmbeddingClient
from knowledge_platform.retrieval.local_transformers_embedding import LocalTransformersEmbeddingClient

_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_OUTPUT_DIR = _ROOT / "artifacts/phase0b-local-catalog"
_DEFAULT_MANIFEST = _DEFAULT_OUTPUT_DIR / "phase8-local-vector-rebuild-manifest.json"
_DEFAULT_CHUNKS = _DEFAULT_OUTPUT_DIR / "phase8-local-vector-chunks.json"
_COLLECTION = "puddingclaw_platform_candidate_text"
_DEFAULT_KEY_ENV = "PUDDINGCLAW_PLATFORM_EMBEDDING_API_KEY"
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
MilvusClient = None


def _milvus_client(*, uri: str, timeout: int) -> Any:
    """Load the optional Milvus provider only when a real client is needed."""

    global MilvusClient
    if MilvusClient is None:
        try:
            from pymilvus import MilvusClient as client_type
        except ModuleNotFoundError as error:
            raise RuntimeError("optional provider pymilvus is required for a Milvus candidate") from error
        MilvusClient = client_type
    return MilvusClient(uri=uri, timeout=timeout)


def _milvus_data_type(name: str) -> Any:
    """Resolve a provider enum lazily; symbolic values keep injected fakes unit-testable."""

    try:
        from pymilvus import DataType
    except ModuleNotFoundError:
        return name
    return getattr(DataType, name)


def _validate_local_endpoint(endpoint: str) -> str:
    """Accept only an explicit HTTP(S) loopback embedding endpoint."""

    parsed = urlsplit(endpoint) if isinstance(endpoint, str) else None
    if (
        parsed is None
        or parsed.scheme not in {"http", "https"}
        or parsed.hostname not in _LOOPBACK_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or endpoint != endpoint.strip()
        or any(ord(character) < 32 for character in endpoint)
    ):
        raise ValueError("local embedding endpoint must be an explicit loopback URL")
    return endpoint


def _load_inputs(manifest_path: Path, chunks_path: Path) -> tuple[VectorRebuildManifest, tuple[VectorRebuildChunk, ...]]:
    for path in (manifest_path, chunks_path):
        absolute = path.expanduser().absolute()
        current = absolute
        while current != current.parent:
            if current.is_symlink():
                raise ValueError("dense candidate input must not contain symlink components")
            current = current.parent
        if not absolute.is_file():
            raise FileNotFoundError(absolute)

    manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest_payload, dict):
        raise VectorRebuildPlanError("dense manifest is invalid")
    raw_documents = manifest_payload.get("documents")
    if not isinstance(raw_documents, list):
        raise VectorRebuildPlanError("dense manifest documents are invalid")
    from knowledge_platform.catalog.vector_rebuild import VectorRebuildDocument

    manifest = VectorRebuildManifest(
        **{
            **manifest_payload,
            "documents": tuple(VectorRebuildDocument(**item) for item in raw_documents),
        }
    )
    if manifest.provider_collection_name != _COLLECTION:
        raise VectorRebuildPlanError("dense manifest is not the dense candidate")
    chunks_payload = json.loads(chunks_path.read_text(encoding="utf-8"))
    if not isinstance(chunks_payload, dict) or chunks_payload.get("manifest_digest") != manifest.manifest_digest():
        raise VectorRebuildPlanError("dense chunks do not match manifest")
    if chunks_payload.get("provider_collection_name") != _COLLECTION:
        raise VectorRebuildPlanError("dense chunks are not for the dense candidate")
    raw_chunks = chunks_payload.get("chunks")
    if not isinstance(raw_chunks, list) or not raw_chunks or any(not isinstance(item, dict) for item in raw_chunks):
        raise VectorRebuildPlanError("dense chunks are invalid")
    chunks = tuple(
        VectorRebuildChunk(
            asset_id=item["asset_id"],
            chunk_id=item["chunk_id"],
            ordinal=item["ordinal"],
            text=item["text"],
            content_digest=item["content_digest"],
            source_revision=item["source_revision"],
        )
        for item in raw_chunks
    )
    documents = {document.asset_id: document for document in manifest.documents}
    if (
        not chunks
        or len({chunk.chunk_id for chunk in chunks}) != len(chunks)
        or {chunk.asset_id for chunk in chunks} != set(documents)
        or any(
            chunk.content_digest != documents[chunk.asset_id].content_digest
            or chunk.source_revision != documents[chunk.asset_id].source_revision
            for chunk in chunks
        )
    ):
        raise VectorRebuildPlanError("dense chunks contain an unbound Asset")
    return manifest, chunks


def _create_schema(client: Any, dimension: int) -> Any:
    schema = client.create_schema(auto_id=False, enable_dynamic_field=False)
    schema.add_field(field_name="id", datatype=_milvus_data_type("VARCHAR"), max_length=255, is_primary=True)
    schema.add_field(field_name="doc_id", datatype=_milvus_data_type("VARCHAR"), max_length=255)
    schema.add_field(field_name="text", datatype=_milvus_data_type("VARCHAR"), max_length=65535)
    # Keep the provenance carried by VectorEmbeddingRow explicit in the
    # candidate schema.  Dynamic fields stay disabled so an insert cannot
    # silently drop or accept unreviewed row attributes.
    schema.add_field(field_name="content_digest", datatype=_milvus_data_type("VARCHAR"), max_length=72)
    schema.add_field(field_name="source_revision", datatype=_milvus_data_type("VARCHAR"), max_length=255)
    schema.add_field(field_name="embedding", datatype=_milvus_data_type("FLOAT_VECTOR"), dim=dimension)
    return schema


def _row_payload(rows: tuple[VectorEmbeddingRow, ...]) -> list[dict[str, object]]:
    return [row.to_dict() for row in rows]


def run_shadow(
    *,
    manifest_path: Path = _DEFAULT_MANIFEST,
    chunks_path: Path = _DEFAULT_CHUNKS,
    output_dir: Path = _DEFAULT_OUTPUT_DIR,
    milvus_uri: str = "http://127.0.0.1:19530",
    endpoint: str | None = None,
    model: str | None = None,
    dimension: int | None = None,
    api_key_env: str = _DEFAULT_KEY_ENV,
    batch_size: int = 32,
    allow_network: bool = False,
    local_endpoint: bool = False,
    from_vault: bool = False,
    local_model_dir: Path | None = None,
    local_model_max_length: int = 1024,
    embedding_checkpoint_path: Path | None = None,
    embed_client: Any | None = None,
    milvus_client: Any | None = None,
) -> dict[str, Any]:
    output_dir = output_dir.expanduser().absolute()
    current = output_dir
    while current != current.parent:
        if current.is_symlink():
            raise ValueError("dense candidate output directory must not contain symlink components")
        current = current.parent
    output_dir.mkdir(parents=True, exist_ok=True)
    output_dir = output_dir.resolve()
    result: dict[str, Any] = {
        "format": "agent-knowledge-platform-phase8-local-dense-candidate/v1",
        "status": "PHASE8_LOCAL_DENSE_CANDIDATE_BLOCKED",
        "activation_allowed": False,
        "source_paths_emitted": False,
        "candidate_collection_name": _COLLECTION,
        "catalog_binding_status": "not_activated",
        "network_contacted": False,
        "embedding_status": "not_attempted",
        "external_network_contacted": False,
    }
    created_candidate = False
    client: Any | None = milvus_client
    injected_provider = embed_client is not None
    owns_embed_client = False
    try:
        manifest, chunks = _load_inputs(manifest_path, chunks_path)
        result.update(
            {
                "manifest_digest": manifest.manifest_digest(),
                "chunk_count": len(chunks),
                "document_count": len(manifest.documents),
                "embedding_dimension": dimension,
                "batch_size": batch_size,
            }
        )
        if local_endpoint:
            if allow_network or from_vault:
                raise ValueError("local endpoint cannot be combined with external network or Vault opt-in")
            if endpoint is None or model is None or type(dimension) is not int:
                raise ValueError("local endpoint requires explicit endpoint, model, and dimension")
            _validate_local_endpoint(endpoint)
        if local_model_dir is not None:
            if allow_network or local_endpoint or from_vault or endpoint is not None or model is not None or embed_client is not None:
                raise ValueError("local model cannot be combined with endpoint, external network, Vault, or injected provider")
            if type(dimension) is not int:
                raise ValueError("local model requires explicit dimension")
        if not allow_network and not local_endpoint and local_model_dir is None and embed_client is None:
            result.update(
                {
                    "status": "PHASE8_LOCAL_DENSE_CANDIDATE_BLOCKED_EXTERNAL_EMBEDDING_NOT_AUTHORIZED",
                    "embedding_status": "not_attempted",
                }
            )
            return result
        client = client or _milvus_client(uri=milvus_uri, timeout=10)
        if client.has_collection(collection_name=_COLLECTION):
            result["error_type"] = "CandidateCollectionAlreadyExists"
            result["status"] = "PHASE8_LOCAL_DENSE_CANDIDATE_BLOCKED_EXISTING_COLLECTION"
            return result
        if embed_client is None:
            if local_endpoint:
                embed_client = OpenAICompatibleEmbeddingClient(
                    endpoint=endpoint,
                    model=model,
                    dimension=dimension,
                    api_key="",
                    batch_size=batch_size,
                )
                owns_embed_client = True
                result["embedding_config_source"] = "explicit_local_endpoint"
                result["embedding_transport"] = "loopback"
            elif local_model_dir is not None:
                embed_client = LocalTransformersEmbeddingClient(
                    model_dir=local_model_dir,
                    dimension=dimension,
                    batch_size=batch_size,
                    max_length=local_model_max_length,
                )
                owns_embed_client = True
                result["embedding_config_source"] = "explicit_local_model"
                result["embedding_transport"] = "in_process"
                result["embedding_provider_version"] = LocalTransformersEmbeddingClient.provider_version
                result["embedding_max_length"] = local_model_max_length
            else:
                if from_vault:
                    from config import get_fallback_embedding_config

                    host_config = get_fallback_embedding_config()
                    endpoint = str(host_config.get("api_base") or "")
                    model = str(host_config.get("model") or "")
                    dimension = int(host_config.get("dimension") or 0)
                    api_key = str(host_config.get("api_key") or "")
                    result["embedding_config_source"] = "host_vault"
                else:
                    api_key = os.environ.get(api_key_env, "")
                if not isinstance(endpoint, str) or not isinstance(model, str) or type(dimension) is not int:
                    raise ValueError("explicit embedding endpoint, model, and dimension are required")
                if not api_key:
                    raise ValueError("embedding API key is empty")
                embed_client = OpenAICompatibleEmbeddingClient(
                    endpoint=endpoint,
                    model=model,
                    dimension=dimension,
                    api_key=api_key,
                    batch_size=batch_size,
                )
                owns_embed_client = True
                result["embedding_endpoint_host"] = urlsplit(endpoint).hostname
        result["embedding_status"] = "started"
        if injected_provider:
            result["embedding_config_source"] = "injected_provider"
        elif isinstance(embed_client, OpenAICompatibleEmbeddingClient):
            result["network_contacted"] = True
            result["external_network_contacted"] = not local_endpoint
        if embedding_checkpoint_path is not None:
            provider_signature = getattr(embed_client, "checkpoint_signature", None)
            if not isinstance(provider_signature, str) or not provider_signature:
                raise ValueError("embedding checkpoint requires a checkpoint-compatible provider")
            rows = build_embedded_rows_checkpointed(
                chunks=chunks,
                embed=embed_client.embed,
                dimension=dimension or 0,
                batch_size=batch_size,
                checkpoint_path=embedding_checkpoint_path,
                manifest_digest=manifest.manifest_digest(),
                provider_signature=provider_signature,
            )
            result["embedding_checkpoint_enabled"] = True
            result["embedding_checkpoint_rows"] = len(rows)
        else:
            rows = build_embedded_rows(
                chunks=chunks,
                embed=embed_client.embed,
                dimension=dimension or 0,
                batch_size=batch_size,
            )
        result["embedding_status"] = "verified"
        if len(rows) != len(chunks):
            raise VectorRebuildPlanError("dense embedding row count does not match chunks")
        schema = _create_schema(client, dimension or 0)
        index_params = client.prepare_index_params()
        index_params.add_index(
            field_name="embedding",
            index_type="AUTOINDEX",
            metric_type="COSINE",
        )
        client.create_collection(collection_name=_COLLECTION, schema=schema, index_params=index_params)
        created_candidate = True
        payload = _row_payload(rows)
        for start in range(0, len(payload), 128):
            client.insert(collection_name=_COLLECTION, data=payload[start : start + 128])
        client.flush(collection_name=_COLLECTION)
        client.load_collection(collection_name=_COLLECTION)
        stats = client.get_collection_stats(collection_name=_COLLECTION)
        row_count = int(stats.get("row_count", 0)) if isinstance(stats, dict) else 0
        if row_count != len(rows):
            raise RuntimeError("dense candidate row count does not match embeddings")
        result.update(
            {
                "row_count": row_count,
                "identity_verification": "all_rows_catalog_asset_bound",
                "status": "PHASE8_LOCAL_DENSE_CANDIDATE_PASS_NOT_ACTIVATABLE",
            }
        )
    except Exception as error:
        result["error_type"] = type(error).__name__
        if created_candidate and client is not None:
            try:
                client.drop_collection(collection_name=_COLLECTION)
                result["candidate_collection_rollback"] = "dropped_after_failed_verification"
            except Exception as cleanup_error:
                result["candidate_collection_rollback"] = "cleanup_failed"
                result["cleanup_error_type"] = type(cleanup_error).__name__
    finally:
        close = getattr(embed_client, "close", None)
        if owns_embed_client and callable(close):
            close()
        report_path = output_dir / "phase8-local-dense-candidate-report.json"
        report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        result["report"] = str(report_path)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=_DEFAULT_MANIFEST)
    parser.add_argument("--chunks", type=Path, default=_DEFAULT_CHUNKS)
    parser.add_argument("--output-dir", type=Path, default=_DEFAULT_OUTPUT_DIR)
    parser.add_argument("--milvus-uri", default="http://127.0.0.1:19530")
    parser.add_argument("--endpoint")
    parser.add_argument("--model")
    parser.add_argument("--dimension", type=int)
    parser.add_argument("--api-key-env", default=_DEFAULT_KEY_ENV)
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--allow-network", action="store_true")
    parser.add_argument(
        "--local-endpoint",
        action="store_true",
        help="use an explicitly supplied HTTP(S) loopback embedding endpoint without Vault or external-network opt-in",
    )
    parser.add_argument("--from-vault", action="store_true", help="explicitly resolve the host embedding config from the local Vault")
    parser.add_argument(
        "--local-model-dir",
        type=Path,
        help="use an explicitly supplied local Transformers model directory without network, endpoint, or Vault access",
    )
    parser.add_argument("--local-model-max-length", type=int, default=1024)
    parser.add_argument(
        "--embedding-checkpoint",
        type=Path,
        help="atomically checkpoint local embedding batches; requires a checkpoint-compatible provider",
    )
    args = parser.parse_args()
    result = run_shadow(
        manifest_path=args.manifest,
        chunks_path=args.chunks,
        output_dir=args.output_dir,
        milvus_uri=args.milvus_uri,
        endpoint=args.endpoint,
        model=args.model,
        dimension=args.dimension,
        api_key_env=args.api_key_env,
        batch_size=args.batch_size,
        allow_network=args.allow_network,
        local_endpoint=args.local_endpoint,
        from_vault=args.from_vault,
        local_model_dir=args.local_model_dir,
        local_model_max_length=args.local_model_max_length,
        embedding_checkpoint_path=args.embedding_checkpoint,
    )
    print(json.dumps({"status": result["status"], "report": result["report"]}, ensure_ascii=False))
    return 0 if str(result["status"]).endswith("NOT_ACTIVATABLE") else 1


if __name__ == "__main__":
    raise SystemExit(main())
