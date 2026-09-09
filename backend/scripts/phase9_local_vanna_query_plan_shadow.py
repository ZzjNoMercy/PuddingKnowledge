"""Compile a QueryPlan from the current local Vanna Collection candidate.

The local PostgreSQL resolver reads only the bound schema/revision.  The
Vanna side is the file-backed Collection gateway; this command issues no SQL
execution and records no database rows.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from knowledge_contracts import Correlation, Principal
from knowledge_platform.database import (
    DatabaseNl2SqlService,
    GatewayVannaProvider,
    InMemoryQueryPlanRepository,
    LocalPostgresDatabaseDatasetResolver,
    LocalPostgresDatabaseSource,
    LocalVannaCollectionGateway,
    PostgresReadonlySqlValidator,
)

_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_OUTPUT = _ROOT / "artifacts/phase9-local-database-vanna-replay/phase9-local-vanna-query-plan-shadow.json"
_DEFAULT_COLLECTIONS = _ROOT / "artifacts/phase9-local-database-vanna-replay/vanna/collections"
_QUESTION = "统计本地车型能源类型数量"
_SPACE_ID = "space_kb_default"
_DATASET_ID = "database_insight_data_vehicle_model_base"
_TABLE = "vehicle_model_base"


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _current_collection(root: Path) -> Path:
    candidates = sorted(path for path in root.glob("*") if path.is_dir() and not path.is_symlink())
    if len(candidates) != 1:
        raise ValueError("local Vanna Collection candidate is not unique")
    return candidates[0]


def run_shadow(
    *,
    collection_root: Path = _DEFAULT_COLLECTIONS,
    output_path: Path = _DEFAULT_OUTPUT,
    host: str = "127.0.0.1",
    port: int = 5432,
    database: str = "insight_data",
    username: str = "pet",
    password: str = "",
) -> dict[str, Any]:
    output_path = output_path.expanduser().absolute()
    result: dict[str, Any] = {
        "format": "agent-knowledge-platform-phase9-local-vanna-query-plan-shadow/v1",
        "status": "PHASE9_LOCAL_VANNA_QUERY_PLAN_SHADOW_FAILED",
        "activation_allowed": False,
        "execution_allowed": False,
        "model_io_performed": False,
        "vector_io_performed": False,
        "database_schema_io_performed": False,
        "database_row_io_performed": False,
        "legacy_collection_read": False,
        "source": {
            "host_digest": _digest(host),
            "database_digest": _digest(database),
            "username_digest": _digest(username),
            "table": _TABLE,
        },
    }
    try:
        source = LocalPostgresDatabaseSource(
            dataset_id=_DATASET_ID,
            space_id=_SPACE_ID,
            host=host,
            port=port,
            database=database,
            username=username,
            password=password,
            allowed_tables=(_TABLE,),
            dataset_version="local-postgresql-v1",
            deployment_revision="local-shadow-v1",
            semantic_context_hash="sha256:" + "0" * 64,
        )
        datasets = LocalPostgresDatabaseDatasetResolver([source])
        binding = datasets.resolve(dataset_id=_DATASET_ID, space_id=_SPACE_ID)
        if binding is None:
            raise LookupError("local PostgreSQL dataset binding is unavailable")
        result["database_schema_io_performed"] = True
        result["source"]["source_revision"] = binding.source_revision
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
        plans = InMemoryQueryPlanRepository()
        generated = DatabaseNl2SqlService(
            datasets=datasets,
            provider=provider,
            validator=PostgresReadonlySqlValidator(),
            plans=plans,
        ).generate(
            principal=Principal(
                subject_id="phase9-local-vanna-query-plan-shadow",
                scopes=("knowledge.database_nl2sql", f"knowledge.space:{_SPACE_ID}"),
            ),
            correlation=Correlation("phase9-local-vanna-query-plan"),
            space_id=_SPACE_ID,
            dataset_id=_DATASET_ID,
            question=_QUESTION,
        )
        if generated.status != "ok":
            raise ValueError("local Vanna candidate did not produce a validated QueryPlan")
        plan = generated.data["query_plan"]
        result.update(
            {
                "status": "PHASE9_LOCAL_VANNA_QUERY_PLAN_SHADOW_PASS_NOT_ACTIVATABLE",
                "query_plan": {
                    "issued": True,
                    "query_plan_id_digest": _digest(str(plan["query_plan_id"])),
                    "sql_digest": _digest(str(plan["sql"])),
                    "validation": dict(plan["validation"]),
                    "evidence_count": len(generated.evidence),
                    "provenance": (
                        {
                            "space_id": generated.provenance.space_id,
                            "dataset_id": generated.provenance.dataset_id,
                            "dataset_version": generated.provenance.dataset_version,
                            "capability": generated.provenance.capability,
                            "provider_versions": dict(generated.provenance.provider_versions),
                        }
                        if generated.provenance
                        else None
                    ),
                },
            }
        )
    except (OSError, TypeError, ValueError, KeyError, LookupError) as error:
        result["failure"] = type(error).__name__
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    result["report"] = str(output_path)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--collection-root", type=Path, default=_DEFAULT_COLLECTIONS)
    parser.add_argument("--output", type=Path, default=_DEFAULT_OUTPUT)
    parser.add_argument("--db-host", default=os.getenv("PUDDINGCLAW_CANONICAL_DB_HOST", "127.0.0.1"))
    parser.add_argument("--db-port", type=int, default=int(os.getenv("PUDDINGCLAW_CANONICAL_DB_PORT", "5432")))
    parser.add_argument("--db-name", default=os.getenv("PUDDINGCLAW_CANONICAL_DB_NAME", "insight_data"))
    parser.add_argument("--db-user", default=os.getenv("PUDDINGCLAW_CANONICAL_DB_USER", "pet"))
    parser.add_argument("--db-password-env", default="PUDDINGCLAW_CANONICAL_DB_PASSWORD")
    args = parser.parse_args()
    result = run_shadow(
        collection_root=args.collection_root,
        output_path=args.output,
        host=args.db_host,
        port=args.db_port,
        database=args.db_name,
        username=args.db_user,
        password=os.getenv(args.db_password_env, ""),
    )
    print(json.dumps({"status": result["status"], "report": result["report"]}, ensure_ascii=False))
    return 0 if result["status"].endswith("NOT_ACTIVATABLE") else 1


if __name__ == "__main__":
    raise SystemExit(main())
