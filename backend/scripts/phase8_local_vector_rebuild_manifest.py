"""Build a path-free candidate Vector rebuild manifest from local Catalog data."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from knowledge_platform.catalog import SqliteCatalogQueryRepository
from knowledge_platform.catalog.vector_rebuild import VectorRebuildPlanError, build_vector_rebuild_manifest

_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_OUTPUT_DIR = _ROOT / "artifacts/phase0b-local-catalog"
_DEFAULT_CATALOG = _DEFAULT_OUTPUT_DIR / "knowledge-platform.sqlite3"
_DEFAULT_SOURCE_ROOTS = (Path("/Users/pet/Documents/knowledge"),)
_STRUCTURED_MIMES = frozenset(
    {
        "text/csv",
        "application/csv",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }
)


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _source_digest_index(roots: tuple[Path, ...]) -> dict[str, list[Path]]:
    result: dict[str, list[Path]] = {}
    for root in roots:
        root = root.expanduser().absolute()
        if root.is_symlink() or not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if ".puddingclaw" in path.parts or path.is_symlink() or not path.is_file():
                continue
            try:
                result.setdefault(_digest(path), []).append(path)
            except OSError:
                continue
    for digest, paths in result.items():
        result[digest] = sorted(
            paths,
            key=lambda path: (
                1 if ".tasks" in path.parts else 0,
                0 if "originals" in path.parts else 1,
                str(path),
            ),
        )
    return result


def run_shadow(
    *,
    catalog: Path = _DEFAULT_CATALOG,
    source_roots: tuple[Path, ...] = _DEFAULT_SOURCE_ROOTS,
    output_dir: Path = _DEFAULT_OUTPUT_DIR,
) -> dict[str, Any]:
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {
        "format": "agent-knowledge-platform-phase8-local-vector-rebuild-manifest/v1",
        "status": "PHASE8_LOCAL_VECTOR_REBUILD_MANIFEST_BLOCKED",
        "activation_allowed": False,
        "source_paths_emitted": False,
        "catalog_asset_count": 0,
        "resolved_asset_count": 0,
        "unresolved_asset_count": 0,
        "ambiguous_same_digest_asset_count": 0,
        "document_asset_count": 0,
        "structured_asset_count": 0,
    }
    try:
        repository = SqliteCatalogQueryRepository(catalog)
        collections = repository.list_collections(space_id="space_kb_default")
        collection = next(item for item in collections if item.get("id") == "dataset_kb_default")
        assets = repository.list_assets(space_id="space_kb_default")
        source_index = _source_digest_index(source_roots)
        document_assets = [
            asset for asset in assets if str(asset.get("mime_type") or "") not in _STRUCTURED_MIMES
        ]
        resolved = [source_index.get(str(asset.get("content_digest") or ""), []) for asset in document_assets]
        result["catalog_asset_count"] = len(assets)
        result["resolved_asset_count"] = sum(bool(paths) for paths in resolved)
        result["unresolved_asset_count"] = sum(not paths for paths in resolved)
        result["ambiguous_same_digest_asset_count"] = sum(len(paths) > 1 for paths in resolved)
        result["document_asset_count"] = len(document_assets)
        result["structured_asset_count"] = result["catalog_asset_count"] - result["document_asset_count"]
        document_collection = {
            **collection,
            "asset_ids": [str(asset["id"]) for asset in document_assets],
        }
        manifest = build_vector_rebuild_manifest(
            catalog_revision=repository.catalog_revision,
            collection=document_collection,
            assets=document_assets,
            provider_collection_name="puddingclaw_platform_candidate_text",
        )
        manifest_path = output_dir / "phase8-local-vector-rebuild-manifest.json"
        manifest_path.write_text(
            json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        result["manifest_path"] = str(manifest_path)
        result["manifest_digest"] = manifest.manifest_digest()
        result["candidate_document_count"] = len(manifest.documents)
        result["stable_provider_identity"] = "catalog_asset_id"
        result["chunker_version"] = manifest.chunker_version
        if result["unresolved_asset_count"] == 0:
            result["status"] = "PHASE8_LOCAL_VECTOR_REBUILD_MANIFEST_PASS_NOT_ACTIVATABLE"
    except (OSError, StopIteration, ValueError, TypeError, VectorRebuildPlanError) as error:
        result["error_type"] = type(error).__name__
    report_path = output_dir / "phase8-local-vector-rebuild-manifest-report.json"
    report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    result["report"] = str(report_path)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=_DEFAULT_CATALOG)
    parser.add_argument("--source-root", action="append", type=Path, dest="source_roots")
    parser.add_argument("--output-dir", type=Path, default=_DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    roots = tuple(args.source_roots) if args.source_roots else _DEFAULT_SOURCE_ROOTS
    result = run_shadow(catalog=args.catalog, source_roots=roots, output_dir=args.output_dir)
    print(json.dumps({"status": result["status"], "report": result["report"]}, ensure_ascii=False))
    return 0 if str(result["status"]).endswith("NOT_ACTIVATABLE") else 1


if __name__ == "__main__":
    raise SystemExit(main())
