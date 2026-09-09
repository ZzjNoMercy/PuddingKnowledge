"""Replay the Platform Wiki Query path against the local published Wiki.

The published Markdown root and Catalog are explicit host inputs.  The script
materializes Wiki page Assets only in a temporary Catalog copy, then runs the
same WikiQueryService and single-engine KnowledgeQueryRouter used by the
public adapters.  Raw snapshots, the canonical Catalog, and the active
runtime are never read or changed.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from knowledge_contracts import Correlation, Principal
from knowledge_platform.catalog import (
    SqliteCatalogQueryRepository,
)
from knowledge_platform.local.catalog import (
    _file_digest as _file_digest,
)
from knowledge_platform.local.catalog import (
    _materialize_catalog as _materialize_catalog,
)
from knowledge_platform.local.catalog import (
    _page_title as _page_title,
)
from knowledge_platform.local.catalog import (
    _safe_pages as _safe_pages,
)
from knowledge_platform.local.catalog import (
    _snapshot_catalog as _snapshot_catalog,
)
from knowledge_platform.retrieval import LocalPublishedWikiProvider, WikiQueryService
from knowledge_platform.router import KnowledgeQueryRequest, KnowledgeQueryRouter, build_local_query_engines

_DEFAULT_OUTPUT_DIR = Path("artifacts/phase0b-local-catalog")
_DEFAULT_CATALOG = _DEFAULT_OUTPUT_DIR / "knowledge-platform.sqlite3"
_SPACE_ID = "space_kb_default"
_DATASET_ID = "dataset_kb_default"
_MAX_PAGES = 5000
_MAX_PAGE_BYTES = 8 * 1024 * 1024
_TITLE_RE = re.compile(r"(?ms)^---\n(?P<frontmatter>.*?)(?:\n---\n|\Z)")


def _path_digest(path: Path | None) -> str | None:
    if path is None:
        return None
    return "sha256:" + hashlib.sha256(str(path.expanduser().absolute()).encode("utf-8")).hexdigest()












def _query_summary(value: Any) -> dict[str, Any]:
    """Keep the rehearsal report portable; do not persist Wiki excerpts."""

    if not isinstance(value, dict):
        return {"status": "invalid"}
    summary: dict[str, Any] = {"status": value.get("status")}
    if value.get("error"):
        error = value["error"]
        if isinstance(error, dict):
            summary["error"] = {"code": error.get("code"), "retryable": error.get("retryable")}
        else:
            summary["error"] = {"code": "unknown"}
        return summary
    data = value.get("data")
    if isinstance(data, dict):
        summary["data"] = {
            key: data[key]
            for key in ("query", "count", "limit", "routing")
            if key in data and key != "query"
        }
    evidence = value.get("evidence")
    if isinstance(evidence, list):
        summary["evidence"] = [
            {
                key: item.get(key)
                for key in ("asset_id", "resource_uri", "locator", "revision", "score", "matched_by")
                if key in item
            }
            for item in evidence
            if isinstance(item, dict)
        ]
    provenance = value.get("provenance")
    if isinstance(provenance, dict):
        summary["provenance"] = {
            key: provenance.get(key)
            for key in ("space_id", "dataset_id", "dataset_version", "capability", "catalog_revision")
            if key in provenance
        }
    return summary


def run_shadow(
    *,
    catalog_path: Path = _DEFAULT_CATALOG,
    wiki_root: Path | None = None,
    output_dir: Path = _DEFAULT_OUTPUT_DIR,
    query: str = "",
    limit: int = 6,
) -> dict[str, Any]:
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {
        "format": "agent-knowledge-platform-phase6-local-wiki-query-shadow/v1",
        "activation": "not-activated",
        "status": "PHASE6_WIKI_QUERY_SHADOW_REJECTED_NO_EXPLICIT_PUBLISHED_ROOT",
        "catalog_path_digest": _path_digest(catalog_path),
        "wiki_root_path_digest": _path_digest(wiki_root),
        "materialized": None,
        "query": None,
    }
    try:
        if wiki_root is None or not query.strip():
            if wiki_root is not None and not query.strip():
                result["status"] = "PHASE6_WIKI_QUERY_SHADOW_FAILED"
                result["error"] = "query is required when an explicit published Wiki root is supplied"
            return _write_report(output_dir, result)
        catalog_path = catalog_path.expanduser().absolute()
        if catalog_path.is_symlink() or not catalog_path.is_file():
            raise OSError("staged Catalog is not a regular file")
        with tempfile.TemporaryDirectory(prefix="phase6-wiki-query-shadow-") as temp_dir:
            temporary_catalog = Path(temp_dir) / "knowledge-platform.sqlite3"
            materialized = _materialize_catalog(catalog_path, temporary_catalog, wiki_root)
            repository = SqliteCatalogQueryRepository(temporary_catalog)
            provider = LocalPublishedWikiProvider(
                catalog=repository,
                asset_paths=materialized["file_bindings"],
            )
            service = WikiQueryService(provider, repository)
            router = KnowledgeQueryRouter(
                catalog=repository,
                engines=build_local_query_engines(wiki=service),
            )
            principal = Principal(
                subject_id="phase6-local-wiki-shadow",
                scopes=("knowledge.query", "knowledge.search", f"knowledge.space:{_SPACE_ID}"),
            )
            shadow_result = asyncio.run(
                router.query(
                    principal=principal,
                    correlation=Correlation("phase6-local-wiki-shadow"),
                    request=KnowledgeQueryRequest(
                        query=query,
                        space_id=_SPACE_ID,
                        collection_id=_DATASET_ID,
                        capability_hint="wiki_query",
                        limit=limit,
                    ),
                )
            )
            result["materialized"] = {
                "pages": materialized["pages"],
                "asset_ids": materialized["asset_ids"],
                "page_digests": materialized["page_digests"],
            }
            result["query"] = _query_summary(shadow_result.to_dict())
        result["status"] = (
            "PHASE6_WIKI_QUERY_SHADOW_PASS_NOT_ACTIVATABLE"
            if result["query"].get("status") == "ok"
            else "PHASE6_WIKI_QUERY_SHADOW_FAILED"
        )
    except (OSError, TypeError, ValueError, UnicodeError, sqlite3.Error):
        result["status"] = "PHASE6_WIKI_QUERY_SHADOW_FAILED"
        result["error"] = "phase6 local Wiki query shadow failed"
    return _write_report(output_dir, result)


def _write_report(output_dir: Path, result: dict[str, Any]) -> dict[str, Any]:
    report_path = output_dir / "phase6-local-wiki-query-shadow-report.json"
    report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    result["report"] = str(report_path)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=_DEFAULT_CATALOG)
    parser.add_argument("--wiki-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=_DEFAULT_OUTPUT_DIR)
    parser.add_argument("--query", required=True)
    parser.add_argument("--limit", type=int, default=6)
    args = parser.parse_args()
    result = run_shadow(
        catalog_path=args.catalog,
        wiki_root=args.wiki_root,
        output_dir=args.output_dir,
        query=args.query,
        limit=args.limit,
    )
    print(json.dumps({"status": result["status"], "report": result["report"]}, ensure_ascii=False))
    return 0 if str(result["status"]).endswith("NOT_ACTIVATABLE") or "REJECTED" in str(result["status"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
