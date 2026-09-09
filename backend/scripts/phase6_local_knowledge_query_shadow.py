"""Run a single-engine ``knowledge_query`` shadow against the local Catalog.

The command accepts only explicit ``ASSET_ID=PATH`` bindings.  It never
infers a file path from Catalog metadata and never activates the runtime.
Without bindings it records the current Collection inventory and refuses the
query as a non-activating shadow.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

from knowledge_contracts import Correlation, Principal
from knowledge_platform.catalog import SqliteCatalogQueryRepository
from knowledge_platform.retrieval import DocumentRetrievalService, LocalDocumentRetrievalProvider
from knowledge_platform.router import KnowledgeQueryRequest, KnowledgeQueryRouter, build_local_query_engines

_DEFAULT_OUTPUT_DIR = Path("artifacts/phase0b-local-catalog")


def _sha256(path: Path) -> str | None:
    if not path.is_file() or path.is_symlink():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _path_digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(str(path.absolute()).encode("utf-8")).hexdigest()


def run_shadow(
    *,
    output_dir: Path = _DEFAULT_OUTPUT_DIR,
    query: str = "",
    space_id: str | None = None,
    collection_id: str | None = None,
    capability_hint: str | None = None,
    asset_bindings: dict[str, Path] | None = None,
) -> dict[str, Any]:
    output_dir = output_dir.expanduser().resolve()
    if output_dir.is_symlink():
        raise OSError("shadow output directory must not be a symlink")
    output_dir.mkdir(parents=True, exist_ok=True)
    database_path = output_dir / "knowledge-platform.sqlite3"
    bindings = {str(asset_id): path.expanduser().absolute() for asset_id, path in (asset_bindings or {}).items()}
    result: dict[str, Any] = {
        "format": "agent-knowledge-platform-knowledge-query-shadow/v1",
        "activation": "not-activated",
        "status": "PHASE6_QUERY_SHADOW_REJECTED_NO_APPROVED_FILE_BINDING",
        "catalog": {"database_path_digest": _path_digest(database_path), "collections": []},
        "file_bindings": [
            {
                "asset_id": asset_id,
                "path_digest": _path_digest(path),
                "bytes": path.stat().st_size if path.is_file() and not path.is_symlink() else None,
                "content_digest": _sha256(path),
            }
            for asset_id, path in sorted(bindings.items())
        ],
        "query": None,
    }
    try:
        repository = SqliteCatalogQueryRepository(database_path)
        collections = repository.list_collections(space_id=space_id)
        result["catalog"]["collections"] = [
            {
                "id": str(item.get("id") or ""),
                "space_id": str(item.get("space_id") or ""),
                "version": str(item.get("version") or ""),
                "capabilities": list(item.get("capabilities") or []),
                "asset_count": len(item.get("asset_ids") or []),
            }
            for item in collections
        ]
        if not bindings:
            return _write_report(output_dir, result)
        if not query.strip():
            result["status"] = "PHASE6_QUERY_SHADOW_FAILED"
            result["error"] = "query is required when explicit bindings are supplied"
            return _write_report(output_dir, result)
        provider = LocalDocumentRetrievalProvider(catalog=repository, asset_paths=bindings)
        document_service = DocumentRetrievalService(provider, repository)
        router = KnowledgeQueryRouter(
            catalog=repository,
            engines=build_local_query_engines(document=document_service),
        )
        selected_space = space_id
        if selected_space is None and collection_id is not None:
            matches = [item for item in collections if str(item.get("id") or "") == collection_id]
            selected_space = str(matches[0].get("space_id") or "") if len(matches) == 1 else None
        if selected_space is None:
            result["status"] = "PHASE6_QUERY_SHADOW_FAILED"
            result["error"] = "space_id is required for an explicit local shadow"
            return _write_report(output_dir, result)
        principal = Principal(
            subject_id="phase6-local-shadow",
            scopes=("knowledge.query", "knowledge.search", f"knowledge.space:{selected_space}"),
        )
        shadow_result = asyncio.run(
            router.query(
                principal=principal,
                correlation=Correlation("phase6-local-shadow"),
                request=KnowledgeQueryRequest(
                    query=query,
                    space_id=selected_space,
                    collection_id=collection_id,
                    capability_hint=capability_hint,
                    limit=10,
                ),
            )
        )
        result["query"] = shadow_result.to_dict()
        result["status"] = (
            "PHASE6_QUERY_SHADOW_PASS_NOT_ACTIVATABLE"
            if shadow_result.status == "ok"
            else "PHASE6_QUERY_SHADOW_FAILED"
        )
    except (OSError, TypeError, ValueError):
        result["status"] = "PHASE6_QUERY_SHADOW_FAILED"
        result["error"] = "phase6 local shadow failed"
    return _write_report(output_dir, result)


def _write_report(output_dir: Path, result: dict[str, Any]) -> dict[str, Any]:
    report_path = output_dir / "phase6-knowledge-query-shadow-report.json"
    report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    result["report"] = str(report_path)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=_DEFAULT_OUTPUT_DIR)
    parser.add_argument("--query", default="")
    parser.add_argument("--space-id")
    parser.add_argument("--collection-id")
    parser.add_argument("--capability-hint")
    parser.add_argument("--asset", action="append", default=[], metavar="ASSET_ID=PATH")
    args = parser.parse_args()
    bindings: dict[str, Path] = {}
    for value in args.asset:
        asset_id, separator, path = str(value).partition("=")
        if not separator or not asset_id or not path:
            parser.error("--asset must use ASSET_ID=PATH")
        bindings[asset_id] = Path(path)
    result = run_shadow(
        output_dir=args.output_dir,
        query=args.query,
        space_id=args.space_id,
        collection_id=args.collection_id,
        capability_hint=args.capability_hint,
        asset_bindings=bindings,
    )
    print(json.dumps({"status": result["status"], "report": result["report"]}, ensure_ascii=False))
    return 0 if str(result["status"]).endswith("NOT_ACTIVATABLE") or "REJECTED" in str(result["status"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
