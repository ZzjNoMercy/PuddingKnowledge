"""Build verified, path-free local text chunks for a Vector candidate."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
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


def run_shadow(
    *,
    catalog: Path = _DEFAULT_CATALOG,
    source_roots: tuple[Path, ...] = _DEFAULT_SOURCE_ROOTS,
    output_dir: Path = _DEFAULT_OUTPUT_DIR,
    max_chars: int = 1200,
) -> dict[str, Any]:
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {
        "format": "agent-knowledge-platform-phase8-local-vector-chunks-shadow/v1",
        "status": "PHASE8_LOCAL_VECTOR_CHUNKS_BLOCKED",
        "activation_allowed": False,
        "source_paths_emitted": False,
        "embedding_status": "not_attempted",
        "pdf_extractor": "pdftotext_with_pypdf_fallback",
    }
    try:
        repository = SqliteCatalogQueryRepository(catalog)
        collection = next(
            item for item in repository.list_collections(space_id="space_kb_default")
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
            provider_collection_name="puddingclaw_platform_candidate_text",
        )
        source_index = _source_digest_index(source_roots)
        source_bytes = {}
        duplicate_count = 0
        for document in manifest.documents:
            matches = source_index.get(document.content_digest, [])
            if not matches:
                raise VectorRebuildPlanError("vector rebuild source is missing")
            duplicate_count += len(matches) > 1
            source_bytes[document.asset_id] = matches[0].read_bytes()
        chunks = build_text_chunks(manifest=manifest, source_bytes=source_bytes, max_chars=max_chars)
        chunks_path = output_dir / "phase8-local-vector-chunks.json"
        chunks_path.write_text(
            json.dumps(
                {
                    "format": "agent-knowledge-platform-phase8-local-vector-chunks/v1",
                    "manifest_digest": manifest.manifest_digest(),
                    "provider_collection_name": manifest.provider_collection_name,
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
        result.update(
            {
                "status": "PHASE8_LOCAL_VECTOR_CHUNKS_PASS_NOT_ACTIVATABLE",
                "manifest_digest": manifest.manifest_digest(),
                "manifest_document_count": len(manifest.documents),
                "chunk_count": len(chunks),
                "source_duplicate_asset_count": duplicate_count,
                "chunks_path": str(chunks_path),
                "embedding_status": "not_attempted",
            }
        )
    except (OSError, StopIteration, ValueError, TypeError, VectorRebuildPlanError) as error:
        result["error_type"] = type(error).__name__
    report_path = output_dir / "phase8-local-vector-chunks-shadow-report.json"
    report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    result["report"] = str(report_path)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=_DEFAULT_CATALOG)
    parser.add_argument("--source-root", action="append", type=Path, dest="source_roots")
    parser.add_argument("--output-dir", type=Path, default=_DEFAULT_OUTPUT_DIR)
    parser.add_argument("--max-chars", type=int, default=1200)
    args = parser.parse_args()
    roots = tuple(args.source_roots) if args.source_roots else _DEFAULT_SOURCE_ROOTS
    result = run_shadow(catalog=args.catalog, source_roots=roots, output_dir=args.output_dir, max_chars=args.max_chars)
    print(json.dumps({"status": result["status"], "report": result["report"]}, ensure_ascii=False))
    return 0 if str(result["status"]).endswith("NOT_ACTIVATABLE") else 1


if __name__ == "__main__":
    raise SystemExit(main())
