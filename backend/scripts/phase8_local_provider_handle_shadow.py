"""Observe local Catalog/Wiki/Vector provider handles without activating them."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from knowledge_platform.catalog.providers import observe_local_provider_handles

_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_OUTPUT_DIR = _ROOT / "artifacts/phase0b-local-catalog"
_DEFAULT_CATALOG = _DEFAULT_OUTPUT_DIR / "knowledge-platform.sqlite3"
_DEFAULT_WIKI_ROOT = _ROOT / "docs"


def run_shadow(
    *,
    catalog: Path = _DEFAULT_CATALOG,
    wiki_root: Path = _DEFAULT_WIKI_ROOT,
    vector_uri: str = "http://127.0.0.1:19530",
    text_collection: str = "puddingclaw_knowledge_text",
    image_collection: str = "puddingclaw_knowledge_image",
    output_dir: Path = _DEFAULT_OUTPUT_DIR,
) -> dict[str, Any]:
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    handles = observe_local_provider_handles(
        catalog_path=catalog,
        wiki_root=wiki_root,
        vector_uri=vector_uri,
        text_collection=text_collection,
        image_collection=image_collection,
    )
    result: dict[str, Any] = {
        "format": "agent-knowledge-platform-phase8-local-provider-handle-shadow/v1",
        "status": "PHASE8_LOCAL_PROVIDER_HANDLE_SHADOW_FAILED",
        "activation_allowed": False,
        "handles": [handle.to_dict() for handle in handles],
        "required_kinds": ["blob", "catalog", "vector_index", "wiki_root"],
        "all_required_observed": all(handle.ready for handle in handles),
    }
    if result["all_required_observed"]:
        result["status"] = "PHASE8_LOCAL_PROVIDER_HANDLE_SHADOW_PASS_NOT_ACTIVATABLE"
    else:
        result["status"] = "PHASE8_LOCAL_PROVIDER_HANDLE_SHADOW_BLOCKED_PROVIDER"
    report_path = output_dir / "phase8-local-provider-handle-shadow-report.json"
    report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    result["report"] = str(report_path)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=_DEFAULT_CATALOG)
    parser.add_argument("--wiki-root", type=Path, default=_DEFAULT_WIKI_ROOT)
    parser.add_argument("--vector-uri", default="http://127.0.0.1:19530")
    parser.add_argument("--text-collection", default="puddingclaw_knowledge_text")
    parser.add_argument("--image-collection", default="puddingclaw_knowledge_image")
    parser.add_argument("--output-dir", type=Path, default=_DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    result = run_shadow(
        catalog=args.catalog,
        wiki_root=args.wiki_root,
        vector_uri=args.vector_uri,
        text_collection=args.text_collection,
        image_collection=args.image_collection,
        output_dir=args.output_dir,
    )
    print(json.dumps({"status": result["status"], "report": result["report"]}, ensure_ascii=False))
    return 0 if str(result["status"]).endswith("NOT_ACTIVATABLE") else 1


if __name__ == "__main__":
    raise SystemExit(main())
