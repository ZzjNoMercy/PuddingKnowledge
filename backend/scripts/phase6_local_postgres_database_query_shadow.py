"""Run a non-activating Database Query shadow against local PostgreSQL.

The connection is host-owned and read-only.  The Platform Catalog is copied to
an isolated temporary directory before Collection binding; the report stores
only digests and bounded results, never credentials or a connection URL.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from knowledge_contracts import Correlation, Evidence, Principal
from knowledge_platform.catalog import SqliteCatalogQueryRepository, SqliteStructuredAssetWriter
from knowledge_platform.database import (
    DatabaseCollectionBindingService,
    DatabaseExecuteReadonlyService,
    DatabaseNl2SqlService,
    DatabaseSqlCandidate,
    InMemoryQueryPlanRepository,
    LocalPostgresDatabaseDatasetResolver,
    LocalPostgresDatabaseSource,
    PostgresReadonlyDatabaseExecutor,
    PostgresReadonlySqlValidator,
    StaticNl2SqlProvider,
)
from knowledge_platform.router import KnowledgeQueryRequest, KnowledgeQueryRouter, build_local_query_engines
from knowledge_platform.transport import RestAdminAdapter, StaticProcessingBindingResolver

_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_OUTPUT_DIR = _ROOT / "artifacts/phase0b-local-catalog/phase6-local-postgres"
_DEFAULT_CATALOG = _ROOT / "artifacts/phase0b-local-catalog/knowledge-platform.sqlite3"
_DATASET_ID = "database_insight_data_vehicle_model_base"
_TABLE = "vehicle_model_base"
_QUESTION = "统计本地车型能源类型数量"


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _write_report(output_dir: Path, report: dict[str, Any]) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "phase6-local-postgres-database-query-shadow-report.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report["report"] = str(path)
    return report


def run_shadow(
    *,
    output_dir: Path = _DEFAULT_OUTPUT_DIR,
    catalog: Path = _DEFAULT_CATALOG,
    host: str = "127.0.0.1",
    port: int = 5432,
    database: str = "insight_data",
    username: str = "pet",
    password: str = "",
) -> dict[str, Any]:
    output_dir = output_dir.expanduser().resolve()
    report: dict[str, Any] = {
        "format": "agent-knowledge-platform-phase6-local-postgresql-database-query-shadow/v1",
        "activation": "not-activated",
        "status": "PHASE6_LOCAL_POSTGRES_DATABASE_QUERY_SHADOW_FAILED",
        "source": {
            "host_digest": _digest(host),
            "database_digest": _digest(database),
            "username_digest": _digest(username),
            "table": _TABLE,
            "source_revision": None,
            "row_count": None,
        },
        "query": None,
    }
    try:
        with tempfile.TemporaryDirectory(prefix="phase6-local-postgres-shadow-") as temp_dir:
            catalog_copy = Path(temp_dir) / "catalog.sqlite3"
            shutil.copy2(catalog.expanduser().absolute(), catalog_copy)
            catalog_repo = SqliteCatalogQueryRepository(catalog_copy)
            collection = next(
                (item for item in catalog_repo.list_collections(space_id="space_kb_default") if item["id"] == "dataset_kb_default"),
                None,
            )
            if collection is None:
                raise LookupError("local shadow Collection is unavailable")
            source = LocalPostgresDatabaseSource(
                dataset_id=_DATASET_ID,
                space_id="space_kb_default",
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
            binding = datasets.resolve(dataset_id=_DATASET_ID, space_id="space_kb_default")
            if binding is None:
                raise LookupError("local PostgreSQL dataset binding is unavailable")
            report["source"]["source_revision"] = binding.source_revision
            admin_result = asyncio.run(
                RestAdminAdapter(
                    authoring=object(),
                    processing=object(),
                    bindings=StaticProcessingBindingResolver({}),
                    database_bindings=DatabaseCollectionBindingService(
                        datasets=datasets,
                        writer=SqliteStructuredAssetWriter(catalog_copy),
                    ),
                ).handle(
                    method="POST",
                    path="/v1/database/bindings",
                    principal=Principal(
                        subject_id="phase6-local-postgres-shadow",
                        scopes=("knowledge:admin", "knowledge:space:space_kb_default"),
                    ),
                    correlation=Correlation("phase6-local-postgres-admin-binding"),
                    body={
                        "collection_id": "dataset_kb_default",
                        "collection_version": str(collection["version"]),
                        "space_id": "space_kb_default",
                        "dataset_id": _DATASET_ID,
                    },
                )
            )
            if admin_result.get("status") != "ok":
                raise LookupError("local PostgreSQL Collection binding was rejected")
            report["admin_binding"] = {
                "status": admin_result["status"],
                "collection_id": "dataset_kb_default",
                "collection_version": str(collection["version"]),
                "space_id": "space_kb_default",
                "dataset_id": _DATASET_ID,
                "provider": "database_nl2sql",
            }
            # The revision scanner is also the bounded, repeatable-read row
            # observation; the exact count is returned by the query below.
            evidence = Evidence(
                asset_id=_DATASET_ID,
                resource_uri=f"knowledge://spaces/space_kb_default/databases/{_DATASET_ID}/schema/{_TABLE}",
                locator={"section": f"table:{_TABLE}"},
                quote="local PostgreSQL schema/data binding",
                matched_by=("explicit_local_postgresql_source",),
            )
            provider = {
                _QUESTION: DatabaseSqlCandidate(
                    sql=f"SELECT energy_type, COUNT(*) AS model_count FROM {_TABLE} GROUP BY energy_type ORDER BY model_count DESC",
                    evidence=(evidence,),
                    provider_version="local-postgresql-shadow",
                )
            }
            plans = InMemoryQueryPlanRepository()
            validator = PostgresReadonlySqlValidator()
            nl2sql = DatabaseNl2SqlService(
                datasets=datasets,
                provider=StaticNl2SqlProvider(provider),
                validator=validator,
                plans=plans,
            )
            principal = Principal(
                subject_id="phase6-local-postgres-shadow",
                scopes=(
                    "knowledge.query",
                    "knowledge.database_nl2sql",
                    "knowledge.database_execute_readonly",
                    "knowledge.space:space_kb_default",
                ),
            )
            routed = asyncio.run(
                KnowledgeQueryRouter(
                    catalog=catalog_repo,
                    engines=build_local_query_engines(database_nl2sql=nl2sql),
                ).query(
                    principal=principal,
                    correlation=Correlation("phase6-local-postgres-route"),
                    request=KnowledgeQueryRequest(
                        query=_QUESTION,
                        space_id="space_kb_default",
                        collection_id="dataset_kb_default",
                        capability_hint="database_nl2sql",
                        limit=20,
                    ),
                )
            )
            if routed.status != "ok":
                report["query"] = {"routed": routed.to_dict()}
                return _write_report(output_dir, report)
            query_plan_id = str(routed.data["query_plan"]["query_plan_id"])
            executed = DatabaseExecuteReadonlyService(
                datasets=datasets,
                plans=plans,
                validator=validator,
                executor=PostgresReadonlyDatabaseExecutor(
                    {("space_kb_default", _DATASET_ID): source},
                    validator=validator,
                    source_revisions={("space_kb_default", _DATASET_ID): binding.source_revision},
                ),
            ).execute(
                principal=principal,
                correlation=Correlation("phase6-local-postgres-execute"),
                space_id="space_kb_default",
                query_plan_id=query_plan_id,
                page_size=20,
            )
            report["query"] = {"routed": routed.to_dict(), "executed": executed.to_dict()}
            if executed.status == "ok":
                report["source"]["row_count"] = executed.data.get("row_count")
                report["status"] = "PHASE6_LOCAL_POSTGRES_DATABASE_QUERY_SHADOW_PASS_NOT_ACTIVATABLE"
    except Exception as error:  # the report must not include DSNs or credential errors
        report["error_type"] = type(error).__name__
    return _write_report(output_dir, report)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=_DEFAULT_OUTPUT_DIR)
    parser.add_argument("--catalog", type=Path, default=_DEFAULT_CATALOG)
    parser.add_argument("--db-host", default=os.getenv("PUDDINGCLAW_CANONICAL_DB_HOST", "127.0.0.1"))
    parser.add_argument("--db-port", type=int, default=int(os.getenv("PUDDINGCLAW_CANONICAL_DB_PORT", "5432")))
    parser.add_argument("--db-name", default=os.getenv("PUDDINGCLAW_CANONICAL_DB_NAME", "insight_data"))
    parser.add_argument("--db-user", default=os.getenv("PUDDINGCLAW_CANONICAL_DB_USER", "pet"))
    parser.add_argument("--db-password-env", default="PUDDINGCLAW_CANONICAL_DB_PASSWORD")
    args = parser.parse_args()
    report = run_shadow(
        output_dir=args.output_dir,
        catalog=args.catalog,
        host=args.db_host,
        port=args.db_port,
        database=args.db_name,
        username=args.db_user,
        password=os.getenv(args.db_password_env, ""),
    )
    print(json.dumps({"status": report["status"], "report": report["report"]}, ensure_ascii=False))
    return 0 if str(report["status"]).endswith("NOT_ACTIVATABLE") else 1


if __name__ == "__main__":
    raise SystemExit(main())
