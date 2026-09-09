"""Run the two-phase local Database Query shadow on temporary Catalog copies.

The source file is explicit and is copied into a temporary directory.  The
Catalog copy receives a temporary Collection binding; the canonical Catalog
and all credentials remain untouched.  The report stores digests, not paths.
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

from knowledge_contracts import Correlation, Evidence, Principal
from knowledge_platform.catalog import SqliteCatalogQueryRepository, SqliteStructuredAssetWriter
from knowledge_platform.database import (
    DatabaseExecuteReadonlyService,
    DatabaseNl2SqlService,
    DatabaseSqlCandidate,
    InMemoryQueryPlanRepository,
    LocalSqliteDatabaseDatasetResolver,
    LocalSqliteDatabaseSource,
    SqliteReadonlyDatabaseExecutor,
    SqliteReadonlySqlValidator,
    StaticNl2SqlProvider,
)
from knowledge_platform.router import KnowledgeQueryRequest, KnowledgeQueryRouter, build_local_query_engines

_DEFAULT_OUTPUT_DIR = Path("artifacts/phase0b-local-catalog")


def _path_digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(str(path.absolute()).encode("utf-8")).hexdigest()


def run_shadow(
    *,
    output_dir: Path = _DEFAULT_OUTPUT_DIR,
    source_database: Path,
    space_id: str,
    collection_id: str,
    dataset_id: str,
    question: str,
) -> dict[str, Any]:
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    canonical_catalog = output_dir / "knowledge-platform.sqlite3"
    result: dict[str, Any] = {
        "format": "agent-knowledge-platform-phase6-local-database-query-shadow/v1",
        "activation": "not-activated",
        "status": "PHASE6_DATABASE_QUERY_SHADOW_FAILED",
        "catalog_path_digest": _path_digest(canonical_catalog),
        "source": {"path_digest": _path_digest(source_database), "source_revision": None, "bytes": None},
        "query": None,
    }
    try:
        source_database = source_database.expanduser().absolute()
        source_stat = source_database.stat()
        result["source"]["bytes"] = source_stat.st_size
        with tempfile.TemporaryDirectory(prefix="phase6-database-shadow-") as temp_dir:
            temp_root = Path(temp_dir)
            catalog_copy = temp_root / "catalog.sqlite3"
            database_copy = temp_root / "source.sqlite3"
            shutil.copy2(canonical_catalog, catalog_copy)
            shutil.copy2(source_database, database_copy)
            source_revision = SqliteReadonlyDatabaseExecutor.file_digest(database_copy)
            result["source"]["source_revision"] = source_revision

            catalog = SqliteCatalogQueryRepository(catalog_copy)
            collections = catalog.list_collections(space_id=space_id)
            collection = next((item for item in collections if str(item.get("id") or "") == collection_id), None)
            if collection is None:
                raise ValueError("Collection was not found")
            writer = SqliteStructuredAssetWriter(catalog_copy)
            writer.bind_collection_provider(
                principal=Principal(
                    subject_id="phase6-local-database-shadow",
                    scopes=("knowledge:processing", f"knowledge.space:{space_id}"),
                ),
                collection_id=collection_id,
                collection_version=str(collection.get("version") or ""),
                space_id=space_id,
                capability="database_nl2sql",
                binding={"dataset_id": dataset_id},
            )

            datasets = LocalSqliteDatabaseDatasetResolver(
                [
                    LocalSqliteDatabaseSource(
                        dataset_id=dataset_id,
                        space_id=space_id,
                        path=database_copy,
                        dataset_version="local-sqlite-v1",
                        deployment_revision="local-shadow-v1",
                        allowed_tables=("knowledge_assets",),
                        semantic_context_hash="sha256:" + "0" * 64,
                        provider_version="local-static-shadow",
                    )
                ]
            )
            binding = datasets.resolve(dataset_id=dataset_id, space_id=space_id)
            if binding is None or binding.source_revision != source_revision:
                raise LookupError("local database source binding could not be established")
            evidence = Evidence(
                asset_id=dataset_id,
                resource_uri=f"knowledge://spaces/{space_id}/databases/{dataset_id}/schema/knowledge_assets",
                locator={"section": "table:knowledge_assets"},
                quote="local SQLite schema binding",
                matched_by=("explicit_local_source",),
            )
            provider = StaticNl2SqlProvider(
                {
                    question: DatabaseSqlCandidate(
                        sql="SELECT kind, COUNT(*) AS asset_count FROM knowledge_assets GROUP BY kind",
                        evidence=(evidence,),
                        provider_version="local-static-shadow",
                    )
                }
            )
            plans = InMemoryQueryPlanRepository()
            validator = SqliteReadonlySqlValidator()
            nl2sql = DatabaseNl2SqlService(
                datasets=datasets,
                provider=provider,
                validator=validator,
                plans=plans,
            )
            principal = Principal(
                subject_id="phase6-local-database-shadow",
                scopes=(
                    "knowledge.query",
                    "knowledge.database_nl2sql",
                    "knowledge.database_execute_readonly",
                    f"knowledge.space:{space_id}",
                ),
            )
            router = KnowledgeQueryRouter(
                catalog=catalog,
                engines=build_local_query_engines(database_nl2sql=nl2sql),
            )
            routed = asyncio.run(
                router.query(
                    principal=principal,
                    correlation=Correlation("phase6-local-database-route"),
                    request=KnowledgeQueryRequest(
                        query=question,
                        space_id=space_id,
                        collection_id=collection_id,
                        capability_hint="database_nl2sql",
                        limit=5,
                    ),
                )
            )
            if routed.status != "ok":
                result["query"] = routed.to_dict()
                return _write_report(output_dir, result)
            query_plan_id = str(routed.data["query_plan"]["query_plan_id"])
            executed = DatabaseExecuteReadonlyService(
                datasets=datasets,
                plans=plans,
                validator=validator,
                executor=SqliteReadonlyDatabaseExecutor(
                    {(space_id, dataset_id): database_copy},
                    validator=validator,
                    source_revisions={(space_id, dataset_id): source_revision},
                ),
            ).execute(
                principal=principal,
                correlation=Correlation("phase6-local-database-execute"),
                space_id=space_id,
                query_plan_id=query_plan_id,
                page_size=20,
            )
            result["query"] = {"routed": routed.to_dict(), "executed": executed.to_dict()}
            result["status"] = (
                "PHASE6_DATABASE_QUERY_SHADOW_PASS_NOT_ACTIVATABLE"
                if executed.status == "ok"
                else "PHASE6_DATABASE_QUERY_SHADOW_FAILED"
            )
    except (OSError, TypeError, ValueError, LookupError) as error:
        result["status"] = "PHASE6_DATABASE_QUERY_SHADOW_FAILED"
        result["error"] = "explicit local database binding or query failed"
        result["error_type"] = type(error).__name__
        result["error_detail"] = str(error)
    return _write_report(output_dir, result)


def _write_report(output_dir: Path, result: dict[str, Any]) -> dict[str, Any]:
    report_path = output_dir / "phase6-local-database-query-shadow-report.json"
    report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    result["report"] = str(report_path)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=_DEFAULT_OUTPUT_DIR)
    parser.add_argument("--source-database", type=Path, required=True)
    parser.add_argument("--space-id", required=True)
    parser.add_argument("--collection-id", required=True)
    parser.add_argument("--dataset-id", required=True)
    parser.add_argument("--question", required=True)
    args = parser.parse_args()
    result = run_shadow(
        output_dir=args.output_dir,
        source_database=args.source_database,
        space_id=args.space_id,
        collection_id=args.collection_id,
        dataset_id=args.dataset_id,
        question=args.question,
    )
    print(json.dumps({"status": result["status"], "report": result["report"]}, ensure_ascii=False))
    return 0 if str(result["status"]).endswith("NOT_ACTIVATABLE") else 1


if __name__ == "__main__":
    raise SystemExit(main())
