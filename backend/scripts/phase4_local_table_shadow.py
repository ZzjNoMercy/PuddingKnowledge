"""Run a read-only Table Query shadow against the staged local Catalog.

The command never infers a physical file from legacy metadata.  Without an
explicit ``--file`` binding it reports the current Structured Asset inventory
and exits as a non-activating shadow.  With an explicit Asset/file pair it
executes the same TableQueryService used by REST/MCP and records only
content-addressed file metadata in the report.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import quote

from knowledge_contracts import Correlation, Principal
from knowledge_platform.catalog import SqliteCatalogQueryRepository
from knowledge_platform.structured import LocalStructuredFileProvider, StructuredQueryProviderError, TableQueryService

_DEFAULT_OUTPUT_DIR = Path("artifacts/phase0b-local-catalog")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _path_digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(str(path).encode("utf-8")).hexdigest()


def _catalog_counts(database_path: Path) -> dict[str, int]:
    if database_path.is_symlink() or not database_path.is_file():
        raise FileNotFoundError(database_path)
    uri = f"file:{quote(str(database_path.absolute()), safe='/')}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        rows = connection.execute(
            "SELECT reference_status, COUNT(*) FROM knowledge_structured_assets GROUP BY reference_status ORDER BY reference_status"
        ).fetchall()
        return {str(status): int(count) for status, count in rows}
    finally:
        connection.close()


def run_table_shadow(
    *,
    output_dir: Path = _DEFAULT_OUTPUT_DIR,
    asset_id: str | None = None,
    file_path: Path | None = None,
    dataset_id: str | None = None,
    source_bindings: Sequence[tuple[str, Path]] | None = None,
    query: str = "sales",
) -> dict[str, object]:
    output_dir = output_dir.expanduser().resolve()
    if output_dir.is_symlink():
        raise OSError("shadow output directory must not be a symlink")
    output_dir.mkdir(parents=True, exist_ok=True)
    database_path = output_dir / "knowledge-platform.sqlite3"
    result: dict[str, object] = {
        "format": "agent-knowledge-platform-table-query-shadow/v1",
        "activation": "not-activated",
        "scope": "staged local Structured Asset Catalog; explicit file binding only",
        "status": "PHASE4_TABLE_SHADOW_REJECTED_NO_APPROVED_FILE_BINDING",
        "catalog": {"database_path_digest": _path_digest(database_path), "structured_asset_status_counts": {}},
        "file_binding": None,
        "query": None,
    }
    try:
        result["catalog"] = {
            "database_path_digest": _path_digest(database_path),
            "structured_asset_status_counts": _catalog_counts(database_path),
        }
        if dataset_id and (asset_id or file_path is not None):
            result["status"] = "PHASE4_TABLE_SHADOW_FAILED"
            result["error"] = "asset and dataset bindings are mutually exclusive"
        elif not asset_id and not dataset_id:
            pass
        elif asset_id and file_path is None:
            result["status"] = "PHASE4_TABLE_SHADOW_REJECTED_NO_APPROVED_FILE_BINDING"
        elif dataset_id and not source_bindings:
            result["status"] = "PHASE4_TABLE_SHADOW_REJECTED_NO_APPROVED_FILE_BINDING"
        else:
            repository = SqliteCatalogQueryRepository(database_path)
            selected_id = dataset_id or asset_id
            asset = repository.get_structured_asset(asset_id=str(selected_id))
            if asset is None:
                result["status"] = "PHASE4_TABLE_SHADOW_FAILED"
                result["error"] = "structured Asset was not found"
            elif str(asset.get("reference_status") or "") not in {"ready", "verified", "active"}:
                result["status"] = "PHASE4_TABLE_SHADOW_REJECTED_REFERENCE_NOT_APPROVED"
                result["error"] = "structured Asset reference is not approved"
            else:
                if dataset_id:
                    bindings = tuple((source_id, path.expanduser().absolute()) for source_id, path in source_bindings or ())
                    result["file_binding"] = {
                        "sources": [
                            {
                                "source_id": source_id,
                                "path_digest": "sha256:" + hashlib.sha256(str(path).encode()).hexdigest(),
                                "bytes": path.stat().st_size if path.is_file() else None,
                                "content_digest": _sha256(path) if path.is_file() else None,
                            }
                            for source_id, path in bindings
                        ]
                    }
                    provider = LocalStructuredFileProvider(
                        asset_paths={},
                        asset_uris={dataset_id: str(asset["source_uri"])},
                        logical_asset_sources={dataset_id: bindings},
                    )
                else:
                    resolved_file = file_path.expanduser().absolute()
                    result["file_binding"] = {
                        "path_digest": "sha256:" + hashlib.sha256(str(resolved_file).encode()).hexdigest(),
                        "bytes": resolved_file.stat().st_size if resolved_file.is_file() else None,
                        "content_digest": _sha256(resolved_file) if resolved_file.is_file() else None,
                    }
                    provider = LocalStructuredFileProvider(
                        asset_paths={str(asset_id): resolved_file},
                        asset_uris={str(asset_id): str(asset["source_uri"])},
                    )
                principal = Principal(
                    subject_id="phase4-local-shadow",
                    scopes=("knowledge:table_query", f"knowledge:space:{asset['space_id']}"),
                )
                shadow_result = asyncio.run(
                    TableQueryService(provider=provider, catalog=repository).query(
                        principal=principal,
                        correlation=Correlation("phase4-local-shadow"),
                        query=query,
                        asset_id=asset_id,
                        dataset_id=dataset_id,
                        space_id=str(asset["space_id"]),
                        limit=5,
                    )
                )
                result["status"] = (
                    "PHASE4_TABLE_QUERY_SHADOW_PASS_NOT_ACTIVATABLE"
                    if shadow_result.status == "ok"
                    else "PHASE4_TABLE_SHADOW_FAILED"
                )
                result["query"] = shadow_result.to_dict()
    except (OSError, StructuredQueryProviderError, sqlite3.Error, TypeError, ValueError):
        result["status"] = "PHASE4_TABLE_SHADOW_FAILED"
        result["error"] = "phase4 local shadow failed"
    report_path = output_dir / "phase4-table-query-shadow-report.json"
    report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    result["report"] = str(report_path)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=_DEFAULT_OUTPUT_DIR)
    selector = parser.add_mutually_exclusive_group()
    selector.add_argument("--asset-id")
    selector.add_argument("--dataset-id")
    parser.add_argument("--file", type=Path)
    parser.add_argument("--source", action="append", default=[], metavar="SOURCE_ID=PATH")
    parser.add_argument("--query", default="sales")
    args = parser.parse_args()
    source_bindings: list[tuple[str, Path]] = []
    for value in args.source:
        source_id, separator, source_path = str(value).partition("=")
        if not separator or not source_id or not source_path:
            parser.error("--source must use SOURCE_ID=PATH")
        source_bindings.append((source_id, Path(source_path)))
    result = run_table_shadow(
        output_dir=args.output_dir,
        asset_id=args.asset_id,
        file_path=args.file,
        dataset_id=args.dataset_id,
        source_bindings=source_bindings,
        query=args.query,
    )
    print(json.dumps({"status": result["status"], "report": result["report"]}, ensure_ascii=False))
    return 0 if str(result["status"]).endswith("NOT_ACTIVATABLE") or "REJECTED" in str(result["status"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
