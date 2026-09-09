"""Bind one explicit local Structured Asset on a Catalog copy and query it.

This is a non-activating Phase 6 shadow.  The canonical Catalog is copied to a
temporary directory, the explicit file is verified by digest/size, and only
the temporary copy is advanced from ``pending`` to ``ready``.  No physical
path is written into Catalog metadata or the report.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

from knowledge_contracts import Correlation, Principal
from knowledge_platform.catalog import SqliteCatalogQueryRepository, SqliteStructuredAssetWriter
from knowledge_platform.router import KnowledgeQueryRequest, KnowledgeQueryRouter, build_local_query_engines
from knowledge_platform.structured import (
    LocalStructuredFileBindingVerifier,
    LocalStructuredFileProvider,
    StructuredAssetBindingRequest,
    StructuredAssetBindingService,
    TableQueryService,
)

_DEFAULT_OUTPUT_DIR = Path("artifacts/phase0b-local-catalog")


def _path_digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(str(path.absolute()).encode("utf-8")).hexdigest()


def run_shadow(
    *,
    output_dir: Path = _DEFAULT_OUTPUT_DIR,
    asset_id: str,
    source_path: Path,
    query: str,
    space_id: str | None = None,
    collection_id: str | None = None,
) -> dict[str, Any]:
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    canonical_database = output_dir / "knowledge-platform.sqlite3"
    result: dict[str, Any] = {
        "format": "agent-knowledge-platform-phase6-local-table-query-shadow/v1",
        "activation": "not-activated",
        "status": "PHASE6_TABLE_SHADOW_FAILED",
        "catalog": {"database_path_digest": _path_digest(canonical_database)},
        "binding": {"asset_id": asset_id, "path_digest": _path_digest(source_path), "content_digest": None, "bytes": None},
        "query": None,
    }
    try:
        source_path = source_path.expanduser().absolute()
        source_stat = source_path.stat()
        result["binding"]["bytes"] = source_stat.st_size
        with tempfile.TemporaryDirectory(prefix="phase6-table-shadow-") as temp_dir:
            temporary_database = Path(temp_dir) / canonical_database.name
            shutil.copy2(canonical_database, temporary_database)
            repository = SqliteCatalogQueryRepository(temporary_database)
            asset = repository.get_structured_asset(asset_id=asset_id)
            if asset is None:
                raise ValueError("Structured Asset was not found")
            selected_space = space_id or str(asset.get("space_id") or "")
            if not selected_space:
                raise ValueError("Structured Asset Space is unavailable")
            selected_collections = repository.list_collections(space_id=selected_space)
            if collection_id is None:
                collection_id = str(selected_collections[0].get("id") or "") if len(selected_collections) == 1 else None
            if not collection_id:
                raise ValueError("collection_id is required when the local Catalog has multiple Collections")
            collection = next(
                (item for item in selected_collections if str(item.get("id") or "") == collection_id), None
            )
            if collection is None:
                raise ValueError("Collection was not found in the requested Space")
            result["binding"]["content_digest"] = LocalStructuredFileBindingVerifier().verify_file(
                path=source_path,
                expected_digest=str(asset.get("content_digest") or ""),
                expected_size_bytes=asset.get("size_bytes") if type(asset.get("size_bytes")) is int else None,
            ).content_digest
            principal = Principal(
                subject_id="phase6-local-table-shadow",
                scopes=(
                    "knowledge.query",
                    "knowledge.table_query",
                    "knowledge.processing",
                    f"knowledge.space:{selected_space}",
                ),
            )
            binding_result = StructuredAssetBindingService(
                catalog=repository,
                verifier=LocalStructuredFileBindingVerifier(),
                writer=SqliteStructuredAssetWriter(temporary_database),
            ).bind(
                principal=principal,
                correlation=Correlation("phase6-local-table-binding"),
                request=StructuredAssetBindingRequest(asset_id=asset_id, space_id=selected_space, path=source_path),
            )
            if binding_result.status != "ok":
                result["binding_result"] = binding_result.to_dict()
                return _write_report(output_dir, result)
            collection_binding = SqliteStructuredAssetWriter(temporary_database).bind_collection_provider(
                principal=principal,
                collection_id=collection_id,
                collection_version=str(collection.get("version") or ""),
                space_id=selected_space,
                capability="table_query",
                binding={"asset_id": asset_id},
            )
            bound_asset = repository.get_structured_asset(asset_id=asset_id)
            if bound_asset is None:
                raise ValueError("bound Structured Asset disappeared")
            provider = LocalStructuredFileProvider(
                asset_paths={asset_id: source_path},
                asset_uris={asset_id: str(bound_asset["source_uri"])},
                asset_sheets={asset_id: bound_asset.get("sheet_name")},
            )
            table_service = TableQueryService(provider=provider, catalog=repository)
            router = KnowledgeQueryRouter(
                catalog=repository,
                engines=build_local_query_engines(table=table_service),
            )
            shadow_result = asyncio.run(
                router.query(
                    principal=principal,
                    correlation=Correlation("phase6-local-table-query"),
                    request=KnowledgeQueryRequest(
                        query=query,
                        space_id=selected_space,
                        collection_id=collection_id,
                        capability_hint="table_query",
                        limit=5,
                    ),
                )
            )
            result["binding_result"] = binding_result.to_dict()
            result["collection_binding"] = collection_binding
            result["query"] = shadow_result.to_dict()
            result["status"] = (
                "PHASE6_TABLE_QUERY_SHADOW_PASS_NOT_ACTIVATABLE"
                if shadow_result.status == "ok"
                else "PHASE6_TABLE_QUERY_SHADOW_FAILED"
            )
    except (OSError, TypeError, ValueError):
        result["status"] = "PHASE6_TABLE_QUERY_SHADOW_FAILED"
        result["error"] = "explicit local table binding or query failed"
    return _write_report(output_dir, result)


def _write_report(output_dir: Path, result: dict[str, Any]) -> dict[str, Any]:
    report_path = output_dir / "phase6-local-table-query-shadow-report.json"
    report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    result["report"] = str(report_path)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=_DEFAULT_OUTPUT_DIR)
    parser.add_argument("--asset-id", required=True)
    parser.add_argument("--file", type=Path, required=True)
    parser.add_argument("--query", required=True)
    parser.add_argument("--space-id")
    parser.add_argument("--collection-id")
    args = parser.parse_args()
    result = run_shadow(
        output_dir=args.output_dir,
        asset_id=args.asset_id,
        source_path=args.file,
        query=args.query,
        space_id=args.space_id,
        collection_id=args.collection_id,
    )
    print(json.dumps({"status": result["status"], "report": result["report"]}, ensure_ascii=False))
    return 0 if str(result["status"]).endswith("NOT_ACTIVATABLE") else 1


if __name__ == "__main__":
    raise SystemExit(main())
