"""Replay the current local Vanna Collection through the Platform provider port.

This is a local, non-executing shadow.  It reads the already staged candidate,
uses the current local database binding metadata, and proves that evidence
and an exact existing NL-SQL example cross the provider boundary without
calling a model, vector service, database, or legacy Collection.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from knowledge_platform.database import DatabaseDatasetBinding, GatewayVannaProvider, LocalVannaCollectionGateway

_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_OUTPUT = _ROOT / "artifacts/phase9-local-database-vanna-replay/phase9-local-vanna-provider-shadow.json"
_DEFAULT_COLLECTIONS = _ROOT / "artifacts/phase9-local-database-vanna-replay/vanna/collections"
_SOURCE_REPORT = _ROOT / "artifacts/phase9-local-database-vanna-replay/phase9-local-database-vanna-package-shadow-report.json"
_QUESTION = "统计本地车型能源类型数量"
_SPACE_ID = "space_kb_default"
_DATASET_ID = "database_insight_data_vehicle_model_base"


def _digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _current_collection(root: Path) -> Path:
    candidates = sorted(path for path in root.glob("*") if path.is_dir() and not path.is_symlink())
    if len(candidates) != 1:
        raise ValueError("local Vanna Collection candidate is not unique")
    return candidates[0]


def run_shadow(*, collection_root: Path = _DEFAULT_COLLECTIONS, output_path: Path = _DEFAULT_OUTPUT) -> dict[str, Any]:
    output_path = output_path.expanduser().absolute()
    result: dict[str, Any] = {
        "format": "agent-knowledge-platform-phase9-local-vanna-provider-shadow/v1",
        "status": "PHASE9_LOCAL_VANNA_PROVIDER_SHADOW_FAILED",
        "activation_allowed": False,
        "execution_allowed": False,
        "model_io_performed": False,
        "vector_io_performed": False,
        "database_io_performed": False,
        "legacy_collection_read": False,
    }
    try:
        source_report = json.loads(_SOURCE_REPORT.read_text(encoding="utf-8"))
        source_revision = str(source_report["source"]["source_revision"])
        binding = DatabaseDatasetBinding(
            dataset_id=_DATASET_ID,
            space_id=_SPACE_ID,
            dataset_version="local-postgresql-v1",
            deployment_revision="local-shadow-v1",
            dialect="postgresql",
            allowed_tables=("vehicle_model_base",),
            semantic_context_hash="sha256:" + "0" * 64,
            source_revision=source_revision,
        )
        collection = _current_collection(collection_root.expanduser().absolute())
        collection_manifest = json.loads((collection / "collection-manifest.json").read_text(encoding="utf-8"))
        provider = GatewayVannaProvider(
            LocalVannaCollectionGateway(
                collection,
                expected_collection_name=collection.name,
                expected_package_revision=str(collection_manifest["package_revision"]),
                expected_input_digest=str(collection_manifest["input_digest"]),
            ),
            version="local-file-backed-collection-shadow",
        )
        candidate = provider.generate(question=_QUESTION, binding=binding, semantic_asset_ids=())
        result.update(
            {
                "status": "PHASE9_LOCAL_VANNA_PROVIDER_SHADOW_PASS_NOT_ACTIVATABLE",
                "collection": {"verified": True, "candidate_name": collection.name},
                "query": {
                    "question_digest": _digest(_QUESTION),
                    "sql_digest": _digest(candidate.sql),
                    "evidence_count": len(candidate.evidence),
                    "evidence_kinds": sorted({item.matched_by[-1] for item in candidate.evidence}),
                    "exact_existing_sql_example": True,
                },
            }
        )
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError, LookupError) as error:
        result["failure"] = type(error).__name__
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    result["report"] = str(output_path)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--collection-root", type=Path, default=_DEFAULT_COLLECTIONS)
    parser.add_argument("--output", type=Path, default=_DEFAULT_OUTPUT)
    args = parser.parse_args()
    result = run_shadow(collection_root=args.collection_root, output_path=args.output)
    print(json.dumps({"status": result["status"], "report": result["report"]}, ensure_ascii=False))
    return 0 if result["status"] == "PHASE9_LOCAL_VANNA_PROVIDER_SHADOW_PASS_NOT_ACTIVATABLE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
