"""Build and verify a local Milvus BM25 candidate from Catalog-owned chunks."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from knowledge_platform.catalog import SqliteCatalogQueryRepository
from knowledge_platform.catalog.vector_rebuild import (
    VectorRebuildPlanError,
    build_text_chunks,
    build_vector_rebuild_manifest,
)
from scripts.phase8_local_vector_rebuild_manifest import (
    _DEFAULT_SOURCE_ROOTS,
    _STRUCTURED_MIMES,
    _source_digest_index,
)

_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_OUTPUT_DIR = _ROOT / "artifacts/phase0b-local-catalog"
_DEFAULT_CATALOG = _DEFAULT_OUTPUT_DIR / "knowledge-platform.sqlite3"
_COLLECTION = "puddingclaw_platform_candidate_lexical_text"
MilvusClient = None


def _milvus_client(*, uri: str, timeout: int) -> Any:
    """Load the optional Milvus provider only when the real client is needed."""

    global MilvusClient
    if MilvusClient is None:
        try:
            from pymilvus import MilvusClient as client_type
        except ModuleNotFoundError as error:
            raise RuntimeError("optional provider pymilvus is required for a lexical candidate") from error
        MilvusClient = client_type
    return MilvusClient(uri=uri, timeout=timeout)


def _milvus_components() -> tuple[Any, Any, Any]:
    """Resolve optional schema components lazily, with symbolic values for injected fakes."""

    try:
        from pymilvus import DataType, Function, FunctionType
    except ModuleNotFoundError:
        return None, None, None
    return DataType, Function, FunctionType


def _candidate_manifest_and_chunks(
    *, catalog: Path, source_roots: tuple[Path, ...], max_chars: int
) -> tuple[Any, tuple[Any, ...], dict[str, int]]:
    repository = SqliteCatalogQueryRepository(catalog)
    collection = next(
        item
        for item in repository.list_collections(space_id="space_kb_default")
        if item.get("id") == "dataset_kb_default"
    )
    assets = repository.list_assets(space_id="space_kb_default")
    document_assets = [
        asset for asset in assets if str(asset.get("mime_type") or "") not in _STRUCTURED_MIMES
    ]
    document_collection = {**collection, "asset_ids": [str(asset["id"]) for asset in document_assets]}
    manifest = build_vector_rebuild_manifest(
        catalog_revision=repository.catalog_revision,
        collection=document_collection,
        assets=document_assets,
        provider_collection_name=_COLLECTION,
    )
    source_index = _source_digest_index(source_roots)
    source_bytes: dict[str, bytes] = {}
    duplicate_count = 0
    for document in manifest.documents:
        matches = source_index.get(document.content_digest, [])
        if not matches:
            raise VectorRebuildPlanError("lexical candidate source is missing")
        duplicate_count += len(matches) > 1
        source_bytes[document.asset_id] = matches[0].read_bytes()
    chunks = build_text_chunks(manifest=manifest, source_bytes=source_bytes, max_chars=max_chars)
    return manifest, chunks, {
        "catalog_asset_count": len(assets),
        "document_asset_count": len(document_assets),
        "structured_asset_count": len(assets) - len(document_assets),
        "source_duplicate_asset_count": duplicate_count,
    }


def _create_schema(client: Any) -> Any:
    data_type, function_type_class, function_types = _milvus_components()
    varchar = getattr(data_type, "VARCHAR", "VARCHAR")
    sparse_float_vector = getattr(data_type, "SPARSE_FLOAT_VECTOR", "SPARSE_FLOAT_VECTOR")
    schema = client.create_schema(auto_id=False, enable_dynamic_field=False)
    schema.add_field(field_name="id", datatype=varchar, max_length=255, is_primary=True)
    schema.add_field(field_name="doc_id", datatype=varchar, max_length=255)
    schema.add_field(
        field_name="text",
        datatype=varchar,
        max_length=65535,
        enable_analyzer=True,
    )
    schema.add_field(field_name="sparse_embedding", datatype=sparse_float_vector)
    if function_type_class is None:
        schema.add_function(SimpleNamespace(
            name="puddingclaw_platform_bm25",
            function_type="BM25",
            input_field_names=["text"],
            output_field_names=["sparse_embedding"],
        ))
    else:
        schema.add_function(
            function_type_class(
                name="puddingclaw_platform_bm25",
                function_type=function_types.BM25,
                input_field_names=["text"],
                output_field_names=["sparse_embedding"],
            )
        )
    return schema


def run_shadow(
    *,
    catalog: Path = _DEFAULT_CATALOG,
    source_roots: tuple[Path, ...] = _DEFAULT_SOURCE_ROOTS,
    output_dir: Path = _DEFAULT_OUTPUT_DIR,
    milvus_uri: str = "http://127.0.0.1:19530",
    max_chars: int = 1200,
    search_limit: int = 5,
) -> dict[str, Any]:
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {
        "format": "agent-knowledge-platform-phase8-local-lexical-candidate/v1",
        "status": "PHASE8_LOCAL_VECTOR_LEXICAL_CANDIDATE_BLOCKED",
        "activation_allowed": False,
        "source_paths_emitted": False,
        "candidate_collection_name": _COLLECTION,
        "dense_embedding_status": "not_attempted",
        "catalog_binding_status": "not_activated",
    }
    client: Any | None = None
    created_candidate = False
    try:
        manifest, chunks, counts = _candidate_manifest_and_chunks(
            catalog=catalog, source_roots=source_roots, max_chars=max_chars
        )
        manifest_path = output_dir / "phase8-local-vector-lexical-manifest.json"
        manifest_path.write_text(
            json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        chunks_path = output_dir / "phase8-local-vector-lexical-chunks.json"
        chunks_path.write_text(
            json.dumps(
                {
                    "format": "agent-knowledge-platform-phase8-local-vector-lexical-chunks/v1",
                    "manifest_digest": manifest.manifest_digest(),
                    "provider_collection_name": _COLLECTION,
                    "chunker_version": manifest.chunker_version,
                    "chunks": [chunk.to_dict() for chunk in chunks],
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        result.update(counts)
        result.update(
            {
                "manifest_path": str(manifest_path),
                "manifest_digest": manifest.manifest_digest(),
                "chunk_path": str(chunks_path),
                "chunk_count": len(chunks),
            }
        )

        client = _milvus_client(uri=milvus_uri, timeout=10)
        if client.has_collection(collection_name=_COLLECTION):
            result["error_type"] = "CandidateCollectionAlreadyExists"
            result["status"] = "PHASE8_LOCAL_VECTOR_LEXICAL_CANDIDATE_BLOCKED_EXISTING_COLLECTION"
            return result
        schema = _create_schema(client)
        index_params = client.prepare_index_params()
        index_params.add_index(
            field_name="sparse_embedding",
            index_type="SPARSE_INVERTED_INDEX",
            metric_type="BM25",
        )
        client.create_collection(collection_name=_COLLECTION, schema=schema, index_params=index_params)
        created_candidate = True
        rows = [{"id": chunk.chunk_id, "doc_id": chunk.asset_id, "text": chunk.text} for chunk in chunks]
        for start in range(0, len(rows), 128):
            client.insert(collection_name=_COLLECTION, data=rows[start : start + 128])
        client.flush(collection_name=_COLLECTION)
        client.load_collection(collection_name=_COLLECTION)
        stats = client.get_collection_stats(collection_name=_COLLECTION)
        row_count = int(stats.get("row_count", 0)) if isinstance(stats, dict) else 0
        result["row_count"] = row_count
        if row_count != len(chunks):
            raise RuntimeError("lexical candidate row count does not match chunks")

        query = chunks[0].text[:120]
        raw_results = client.search(
            collection_name=_COLLECTION,
            data=[query],
            anns_field="sparse_embedding",
            limit=search_limit,
            output_fields=["id", "doc_id", "text"],
        )
        hits = raw_results[0] if isinstance(raw_results, list) and raw_results else []
        if not isinstance(hits, list) or not hits:
            raise RuntimeError("lexical candidate search returned no hits")
        chunk_ids = {chunk.chunk_id for chunk in chunks}
        asset_ids = {document.asset_id for document in manifest.documents}
        verified = 0
        for hit in hits:
            entity = hit.get("entity", hit) if isinstance(hit, dict) else hit
            if not isinstance(entity, dict):
                raise RuntimeError("lexical candidate search returned an invalid hit")
            if entity.get("id") not in chunk_ids or entity.get("doc_id") not in asset_ids:
                raise RuntimeError("lexical candidate search returned an unbound identity")
            if any(marker in json.dumps(entity, ensure_ascii=False) for marker in ("/Users/", "file://", "source_path")):
                raise RuntimeError("lexical candidate search returned a physical source path")
            verified += 1
        result.update(
            {
                "status": "PHASE8_LOCAL_VECTOR_LEXICAL_CANDIDATE_PASS_NOT_ACTIVATABLE",
                "search_query_length": len(query),
                "search_result_count": verified,
                "identity_verification": "all_hits_catalog_asset_bound",
                "dense_embedding_status": "not_attempted",
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
        report_path = output_dir / "phase8-local-vector-lexical-candidate-report.json"
        report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        result["report"] = str(report_path)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=_DEFAULT_CATALOG)
    parser.add_argument("--source-root", action="append", type=Path, dest="source_roots")
    parser.add_argument("--output-dir", type=Path, default=_DEFAULT_OUTPUT_DIR)
    parser.add_argument("--milvus-uri", default="http://127.0.0.1:19530")
    parser.add_argument("--max-chars", type=int, default=1200)
    parser.add_argument("--search-limit", type=int, default=5)
    args = parser.parse_args()
    roots = tuple(args.source_roots) if args.source_roots else _DEFAULT_SOURCE_ROOTS
    result = run_shadow(
        catalog=args.catalog,
        source_roots=roots,
        output_dir=args.output_dir,
        milvus_uri=args.milvus_uri,
        max_chars=args.max_chars,
        search_limit=args.search_limit,
    )
    print(json.dumps({"status": result["status"], "report": result["report"]}, ensure_ascii=False))
    return 0 if str(result["status"]).endswith("NOT_ACTIVATABLE") else 1


if __name__ == "__main__":
    raise SystemExit(main())
