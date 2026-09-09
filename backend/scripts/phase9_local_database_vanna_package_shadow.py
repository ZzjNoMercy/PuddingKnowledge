"""Build a portable local Database Package and an inactive Vanna Collection candidate.

The source is the currently bound local PostgreSQL schema.  This shadow reads
schema metadata only; it does not export rows, credentials, connection URLs,
or an existing Vanna/Milvus collection.  The curated DDL is accepted only
when the observed allowlisted table has the expected column contract.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from knowledge_platform.database import (
    DatabaseSchemaTable,
    LocalPostgresDatabaseDatasetResolver,
    LocalPostgresDatabaseSchemaReader,
    LocalPostgresDatabaseSource,
)
from knowledge_platform.package import KnowledgePackageBuilder, validate_package
from scripts.phase9_local_vanna_collection_shadow import run_vanna_collection_shadow

_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_OUTPUT_DIR = _ROOT / "artifacts/phase9-local-database-vanna"
_DEFAULT_CATALOG = _ROOT / "artifacts/phase0b-local-catalog/knowledge-platform.sqlite3"
_SPACE_ID = "space_kb_default"
_DATASET_ID = "database_insight_data_vehicle_model_base"
_SOURCE_ID = "database_source_insight_data_vehicle_model_base"
_TABLE = "public.vehicle_model_base"
_QUESTION = "统计本地车型能源类型数量"
_SQL = "SELECT energy_type, COUNT(*) AS model_count FROM vehicle_model_base GROUP BY energy_type ORDER BY model_count DESC"
_DDL_PATH = _ROOT / "backend/scripts/refresh_vehicle_model_base.sql"
_EXPECTED_COLUMNS = (
    "brand",
    "serial_name",
    "car_name",
    "car_name_full_year",
    "launch_date",
    "launch_year",
    "launch_month",
    "energy_type",
    "vehicle_level",
    "wheelbase_mm",
    "motor_power_kw",
    "price",
    "price_band",
    "sale_status",
    "sale_status_matched",
    "sale_status_source",
    "refreshed_at",
)
_DDL_RE = re.compile(
    r"CREATE TABLE IF NOT EXISTS public\.vehicle_model_base\s*\((?P<body>.*?)\);",
    re.IGNORECASE | re.DOTALL,
)
_COLUMN_LINE_RE = re.compile(r"^\s{2}([A-Za-z][A-Za-z0-9_$-]*)\s+", re.MULTILINE)
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _digest(value: object) -> str:
    payload = value if isinstance(value, bytes) else str(value).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _curated_ddl(columns: tuple[str, ...] = _EXPECTED_COLUMNS) -> str:
    text = _DDL_PATH.read_text(encoding="utf-8")
    match = _DDL_RE.search(text)
    if match is None:
        raise ValueError("curated vehicle_model_base DDL is unavailable")
    body_lines = match.group("body").splitlines()
    column_lines = {
        item.group(1): line.rstrip()
        for line in body_lines
        if (item := _COLUMN_LINE_RE.match(line)) is not None and item.group(1) in _EXPECTED_COLUMNS
    }
    primary_line = next((line.rstrip() for line in body_lines if line.strip().startswith("PRIMARY KEY")), None)
    if tuple(column_lines) != _EXPECTED_COLUMNS or primary_line is None:
        raise ValueError("curated vehicle_model_base DDL column contract is invalid")
    if set(columns) != set(_EXPECTED_COLUMNS) or len(columns) != len(_EXPECTED_COLUMNS):
        raise ValueError("curated vehicle_model_base DDL cannot represent observed columns")
    return "CREATE TABLE public.vehicle_model_base (\n" + "\n".join(
        [*(column_lines[column] for column in columns), primary_line]
    ) + "\n);"


def _database_source(schema: DatabaseSchemaTable) -> dict[str, Any]:
    if schema.table_name.casefold() != _TABLE.casefold() or set(schema.columns) != set(_EXPECTED_COLUMNS):
        raise ValueError("observed local schema does not match the curated Package contract")
    ddl = _curated_ddl(schema.columns)
    aliases = {
        "energy_type": ["能源类型"],
        "vehicle_level": ["级别"],
        "launch_date": ["上市时间"],
        "sale_status": ["销售状态"],
        "wheelbase_mm": ["轴距", "轴距[mm]"],
        "motor_power_kw": ["电动机总功率", "电动机总功率[kW]"],
        "price": ["厂商指导价", "价格"],
    }
    entities = [
        {
            "id": f"entity_vehicle_model_base_{column}",
            "canonical_name": column,
            "entity_type": "column",
            "table_column": f"vehicle_model_base.{column}",
            "aliases": aliases.get(column, []),
        }
        for column in schema.columns
    ]
    return {
        "id": _SOURCE_ID,
        "space_id": _SPACE_ID,
        "dataset_id": _DATASET_ID,
        "dialect": "postgresql",
        "ddl": [{"id": "ddl_vehicle_model_base_v1", "content": ddl}],
        "documentation": [
            {
                "id": "doc_vehicle_model_base_grain_v1",
                "content": (
                    "vehicle_model_base 是车型粒度的本地结构化快照。"
                    "每行由 brand、serial_name、car_name 定位；energy_type 表示能源类型，"
                    "可用于按能源类型统计车型数量。查询必须使用已绑定的 allowlisted table。"
                ),
            }
        ],
        "sql_examples": [{"id": "sql_vehicle_model_base_energy_count_v1", "question": _QUESTION, "sql": _SQL}],
        "entities": entities,
    }


def _package_revision(*, catalog_digest: str, schema: DatabaseSchemaTable, database_source: dict[str, Any]) -> str:
    payload = json.dumps(
        {"catalog_digest": catalog_digest, "schema_revision": schema.schema_revision, "source": database_source},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return _digest(payload)


def _source_from_args(*, host: str, port: int, database: str, username: str, password: str) -> LocalPostgresDatabaseSource:
    return LocalPostgresDatabaseSource(
        dataset_id=_DATASET_ID,
        space_id=_SPACE_ID,
        host=host,
        port=port,
        database=database,
        username=username,
        password=password,
        allowed_tables=("vehicle_model_base",),
        dataset_version="local-postgresql-v1",
        deployment_revision="local-shadow-v1",
        semantic_context_hash="sha256:" + "0" * 64,
    )


def run_shadow(
    *,
    catalog: Path = _DEFAULT_CATALOG,
    output_dir: Path = _DEFAULT_OUTPUT_DIR,
    host: str = "127.0.0.1",
    port: int = 5432,
    database: str = "insight_data",
    username: str = "pet",
    password: str = "",
) -> dict[str, Any]:
    catalog = catalog.expanduser().absolute()
    output_dir = output_dir.expanduser().absolute()
    report_path = output_dir / "phase9-local-database-vanna-package-shadow-report.json"
    report: dict[str, Any] = {
        "format": "agent-knowledge-platform-phase9-local-database-vanna-package-shadow/v1",
        "status": "PHASE9_LOCAL_DATABASE_VANNA_PACKAGE_SHADOW_FAILED",
        "activation": "not-activated",
        "activation_allowed": False,
        "provider_io_performed": False,
        "legacy_collection_read": False,
        "source": {
            "host_digest": _digest(host),
            "database_digest": _digest(database),
            "username_digest": _digest(username),
            "dataset_id": _DATASET_ID,
            "table": _TABLE,
            "source_revision": None,
        },
        "package": {"created": False, "validated": False},
        "collection": {"created": False, "active": False, "activation_allowed": False},
    }
    try:
        catalog_digest = _digest(catalog.read_bytes())
        source = _source_from_args(host=host, port=port, database=database, username=username, password=password)
        resolver = LocalPostgresDatabaseDatasetResolver([source])
        binding = resolver.resolve(dataset_id=_DATASET_ID, space_id=_SPACE_ID)
        if binding is None:
            raise LookupError("local PostgreSQL binding is unavailable")
        report["source"]["source_revision"] = binding.source_revision
        schema_items = LocalPostgresDatabaseSchemaReader({(_SPACE_ID, _DATASET_ID): source}).read(binding=binding)
        if len(schema_items) != 1:
            raise ValueError("local PostgreSQL allowlist returned an unexpected table count")
        schema = schema_items[0]
        database_source = _database_source(schema)
        package_dir = output_dir / "package"
        package = KnowledgePackageBuilder().build(
            output_dir=package_dir,
            package_id="package_local_database_vanna",
            version="local-schema-v1",
            spaces=[{"id": _SPACE_ID, "name": "Local Knowledge", "description": "Local-only knowledge source"}],
            collections=[
                {
                    "id": _DATASET_ID,
                    "space_id": _SPACE_ID,
                    "name": "Local vehicle model database",
                    "version": "local-schema-v1",
                    "kind": "relational-analytics",
                    "asset_ids": [],
                    "capabilities": ["database_nl2sql"],
                }
            ],
            assets=[],
            asset_files={},
            capabilities=["database_nl2sql"],
            catalog_revision=_package_revision(catalog_digest=catalog_digest, schema=schema, database_source=database_source),
            database_sources=[database_source],
        )
        validation = validate_package(package.package_root)
        report["package"] = {
            "created": True,
            "validated": True,
            "package_revision": validation.package_revision,
            "asset_count": validation.asset_count,
            "file_count": validation.file_count,
            "database_source_count": 1,
            "ddl_count": len(database_source["ddl"]),
            "documentation_count": len(database_source["documentation"]),
            "sql_example_count": len(database_source["sql_examples"]),
            "entity_count": len(database_source["entities"]),
        }
        vanna_report = run_vanna_collection_shadow(package_root=package.package_root, output_dir=output_dir / "vanna")
        report["collection"] = {
            "created": vanna_report.get("status") == "PHASE9_LOCAL_VANNA_COLLECTION_REBUILD_PASS_NOT_ACTIVATABLE",
            "name": vanna_report.get("collection", {}).get("name"),
            "counts": vanna_report.get("collection", {}).get("counts", {}),
            "active": False,
            "activation_allowed": False,
            "file_digests_present": _collection_file_digests_present(output_dir / "vanna"),
        }
        if not report["collection"]["created"] or not report["collection"]["file_digests_present"]:
            raise RuntimeError("local Vanna Collection candidate was not created with file digests")
        report["status"] = "PHASE9_LOCAL_DATABASE_VANNA_PACKAGE_SHADOW_PASS_NOT_ACTIVATABLE"
    except (OSError, LookupError, PermissionError, RuntimeError, TypeError, ValueError, json.JSONDecodeError) as error:
        report["failure"] = type(error).__name__
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report["report"] = str(report_path)
    return report


def _collection_file_digests_present(vanna_root: Path) -> bool:
    manifests = list((vanna_root / "collections").glob("*/collection-manifest.json")) if (vanna_root / "collections").is_dir() else []
    if len(manifests) != 1:
        return False
    manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    files = manifest.get("file_digests")
    if not isinstance(files, dict) or set(files) != {
        "ddl.jsonl",
        "documentation.jsonl",
        "entities.jsonl",
        "sql_examples.jsonl",
    }:
        return False
    for name, expected in files.items():
        candidate = manifests[0].parent / name
        if (
            not isinstance(expected, str)
            or _DIGEST_RE.fullmatch(expected) is None
            or candidate.is_symlink()
            or not candidate.is_file()
        ):
            return False
        if _digest(candidate.read_bytes()) != expected:
            return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=_DEFAULT_CATALOG)
    parser.add_argument("--output-dir", type=Path, default=_DEFAULT_OUTPUT_DIR)
    parser.add_argument("--db-host", default=os.getenv("PUDDINGCLAW_CANONICAL_DB_HOST", "127.0.0.1"))
    parser.add_argument("--db-port", type=int, default=int(os.getenv("PUDDINGCLAW_CANONICAL_DB_PORT", "5432")))
    parser.add_argument("--db-name", default=os.getenv("PUDDINGCLAW_CANONICAL_DB_NAME", "insight_data"))
    parser.add_argument("--db-user", default=os.getenv("PUDDINGCLAW_CANONICAL_DB_USER", "pet"))
    parser.add_argument("--db-password-env", default="PUDDINGCLAW_CANONICAL_DB_PASSWORD")
    args = parser.parse_args()
    result = run_shadow(
        catalog=args.catalog,
        output_dir=args.output_dir,
        host=args.db_host,
        port=args.db_port,
        database=args.db_name,
        username=args.db_user,
        password=os.getenv(args.db_password_env, ""),
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "PHASE9_LOCAL_DATABASE_VANNA_PACKAGE_SHADOW_PASS_NOT_ACTIVATABLE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
