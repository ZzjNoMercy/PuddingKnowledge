"""Build a vehicle-series Crosswalk from the real local canonical database.

This is a local, non-activating shadow only.  The canonical entity universe is
read from the local ``vehicle_model_base`` table; the bounded spreadsheet is a
source snapshot and is never promoted to canonical by this script.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import asyncpg
import pandas as pd

from knowledge_platform.semantic import (
    CrosswalkCompositionError,
    LocalCrosswalkPublisher,
    build_vehicle_series_collision_policy_template,
    build_vehicle_series_crosswalk,
    find_vehicle_series_canonical_collisions,
    validate_active_crosswalk,
)

_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_OUTPUT_DIR = _ROOT / "artifacts/phase0b-local-catalog/phase7-local-crosswalk-real"
_DEFAULT_CATALOG = _ROOT / "artifacts/phase0b-local-catalog/knowledge-platform.sqlite3"
_DEFAULT_FILE = Path("/Users/pet/Documents/knowledge/imported/20260710/2023年7月乘用车市场上险量.xlsx")
_CANONICAL_SOURCE_REF = "database:dbs_77982e981bac4a6fa8:vehicle_model_base"
_SOURCE_REF = "table_asset:tbl_da58c9ee25ba7a6a44efef65"


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _path_digest(path: Path) -> str:
    value = str(path.expanduser().absolute()).encode("utf-8")
    return "sha256:" + hashlib.sha256(value).hexdigest()


async def _read_canonical_rows(
    *, host: str, port: int, database: str, user: str
) -> tuple[list[dict[str, str]], list[str]]:
    connection = await asyncpg.connect(host=host, port=port, database=database, user=user)
    try:
        rows = await connection.fetch(
            """
            SELECT brand, serial_name
            FROM vehicle_model_base
            WHERE brand IS NOT NULL AND serial_name IS NOT NULL
            ORDER BY brand, serial_name
            """
        )
        columns = await connection.fetch(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'vehicle_model_base'
            ORDER BY ordinal_position
            """
        )
    finally:
        await connection.close()
    return (
        [
            {"brand": str(row["brand"]).strip(), "serial_name": str(row["serial_name"]).strip()}
            for row in rows
            if str(row["brand"] or "").strip() and str(row["serial_name"] or "").strip()
        ],
        [str(column["column_name"]) for column in columns],
    )


def _read_source_rows(path: Path, *, row_limit: int) -> tuple[list[dict[str, str]], int]:
    if row_limit < 1 or row_limit > 5000:
        raise ValueError("source row limit must be between 1 and 5000")
    frame = pd.read_excel(path, usecols=lambda name: str(name) in {"品牌", "1-子车型"}, nrows=row_limit)
    required = {"品牌", "1-子车型"}
    if not required <= set(frame.columns):
        raise ValueError("local spreadsheet lacks vehicle-series source columns")
    rows: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for row in frame.fillna("").itertuples(index=False):
        values = tuple(str(value).strip() for value in row)
        if len(values) != 2 or not all(values) or values in seen:
            continue
        seen.add(values)
        rows.append({"品牌": values[0], "1-子车型": values[1]})
    if not rows:
        raise ValueError("local spreadsheet does not provide bounded vehicle-series rows")
    return rows, len(frame)


def run_shadow(
    *,
    output_dir: Path,
    catalog: Path,
    source_path: Path,
    canonical_rows: list[dict[str, str]],
    canonical_columns: list[str],
    source_rows: list[dict[str, str]],
    source_rows_read: int,
    source_row_limit: int,
    canonical_database: str,
    canonical_collision_policy: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    output_dir = output_dir.expanduser().resolve()
    catalog = catalog.expanduser().absolute()
    source_path = source_path.expanduser().absolute()
    result: dict[str, Any] = {
        "format": "agent-knowledge-platform-phase7-local-crosswalk-real-canonical-shadow/v1",
        "activation": "not-activated",
        "status": "PHASE7_CROSSWALK_REAL_CANONICAL_SHADOW_REJECTED",
        "canonical_source": {
            "source_ref": _CANONICAL_SOURCE_REF,
            "database_name": canonical_database,
            "table": "vehicle_model_base",
            "row_count": len(canonical_rows),
            "columns": canonical_columns,
            "stable_identity_columns": [
                column for column in canonical_columns if column == "id" or column.endswith("_id")
            ],
            "identity_basis": ["brand", "serial_name"],
        },
        "source_snapshot": {
            "source_ref": _SOURCE_REF,
            "path_digest": _path_digest(source_path),
            "content_digest": _file_digest(source_path),
            "bytes": source_path.stat().st_size,
            "bounded_row_limit": source_row_limit,
            "rows_read": source_rows_read,
            "distinct_rows": len(source_rows),
        },
        "canonical_collision_resolution": {
            "policy_supplied": canonical_collision_policy is not None,
            "policy_format": "JSON normalized_key -> observed candidate sha256",
            "policy_required_when_collisions_exist": True,
        },
        "canonical_catalog_unchanged": None,
        "artifact": None,
    }
    before = _file_digest(catalog)
    publication_root = output_dir / "publication"
    files_before = list((publication_root / "crosswalks").rglob("*.json")) if publication_root.is_dir() else []
    collisions = find_vehicle_series_canonical_collisions(canonical_rows)
    result["canonical_collision_policy_template"] = build_vehicle_series_collision_policy_template(canonical_rows)
    if collisions and canonical_collision_policy is None:
        result["status"] = "PHASE7_CROSSWALK_REAL_CANONICAL_SHADOW_BLOCKED_DATA_QUALITY"
        result["blocker"] = "canonical_normalized_collision"
        result["canonical_collision_count"] = len(collisions)
        result["canonical_collision_summaries"] = [
            {
                "normalized_key_digest": "sha256:" + hashlib.sha256(
                    str(item["normalized_key"]).encode("utf-8")
                ).hexdigest(),
                "candidate_count": item["candidate_count"],
                "candidate_digests": item["candidate_digests"],
            }
            for item in collisions
        ]
        result["canonical_catalog_unchanged"] = before == _file_digest(catalog)
        output_dir.mkdir(parents=True, exist_ok=True)
        report_path = output_dir / "phase7-local-crosswalk-real-canonical-shadow-report.json"
        report_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        result["report"] = str(report_path)
        return result
    try:
        generated = build_vehicle_series_crosswalk(
            canonical_rows=canonical_rows,
            source_rows=source_rows,
            canonical_source_ref=_CANONICAL_SOURCE_REF,
            source_ref=_SOURCE_REF,
            canonical_collision_policy=canonical_collision_policy,
        )
    except CrosswalkCompositionError as exc:
        result["status"] = "PHASE7_CROSSWALK_REAL_CANONICAL_SHADOW_BLOCKED_DATA_QUALITY"
        result["blocker"] = "canonical_normalized_collision"
        result["blocker_detail_digest"] = "sha256:" + hashlib.sha256(str(exc).encode("utf-8")).hexdigest()
        result["canonical_catalog_unchanged"] = before == _file_digest(catalog)
        output_dir.mkdir(parents=True, exist_ok=True)
        report_path = output_dir / "phase7-local-crosswalk-real-canonical-shadow-report.json"
        report_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        result["report"] = str(report_path)
        return result
    publisher = LocalCrosswalkPublisher(root=publication_root)
    first = publisher.publish(
        space_id="space_kb_default",
        dimension_id="vehicle_series",
        generated=generated,
    )
    second = publisher.publish(
        space_id="space_kb_default",
        dimension_id="vehicle_series",
        generated=generated,
    )
    files = list((publication_root / "crosswalks").rglob("*.json"))
    if len(files) != 1:
        raise RuntimeError("real Crosswalk publication was not content-addressed exactly once")
    active = json.loads(files[0].read_text(encoding="utf-8"))
    validate_active_crosswalk(active)
    after = _file_digest(catalog)
    result["canonical_catalog_unchanged"] = before == after
    result["artifact"] = {
        "resource_uri": first.resource_uri,
        "content_digest": first.content_digest,
        "canonical_entity_count": first.canonical_entity_count,
        "canonical_with_source_binding_count": generated["summary"]["canonical_with_source_binding_count"],
        "source_matched_row_count": generated["summary"]["source_matched_row_count"],
        "source_diagnostic_count": len(active["source_diagnostics"]),
        "overrides_applied": first.overrides_applied,
        "second_matches_first": first == second,
        "artifact_preexisted": bool(files_before),
        "publication_count": publisher.publish_count,
        "validated": True,
    }
    if not result["canonical_catalog_unchanged"] or not result["artifact"]["second_matches_first"]:
        raise RuntimeError("Crosswalk shadow changed canonical Catalog or was not idempotent")
    if publisher.publish_count != (0 if files_before else 1):
        raise RuntimeError("Crosswalk publication count does not match content-addressed state")
    result["status"] = "PHASE7_CROSSWALK_REAL_CANONICAL_SHADOW_PASS_NOT_ACTIVATABLE"
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / "phase7-local-crosswalk-real-canonical-shadow-report.json"
    report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    result["report"] = str(report_path)
    return result


async def _async_main(args: argparse.Namespace) -> int:
    canonical_rows, canonical_columns = await _read_canonical_rows(
        host=args.db_host,
        port=args.db_port,
        database=args.db_name,
        user=args.db_user,
    )
    source_path = args.file.expanduser().absolute()
    source_rows, source_rows_read = _read_source_rows(source_path, row_limit=args.source_row_limit)
    collision_policy = None
    if args.canonical_collision_policy is not None:
        try:
            raw_policy = json.loads(args.canonical_collision_policy.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("canonical collision policy is not valid JSON") from exc
        if not isinstance(raw_policy, dict) or any(
            not isinstance(key, str) or not isinstance(value, str) for key, value in raw_policy.items()
        ):
            raise ValueError("canonical collision policy must map string keys to string candidate digests")
        collision_policy = raw_policy
    outcome = run_shadow(
        output_dir=args.output_dir,
        catalog=args.catalog,
        source_path=source_path,
        canonical_rows=canonical_rows,
        canonical_columns=canonical_columns,
        source_rows=source_rows,
        source_rows_read=source_rows_read,
        source_row_limit=args.source_row_limit,
        canonical_database=args.db_name,
        canonical_collision_policy=collision_policy,
    )
    print(json.dumps({"status": outcome["status"], "report": outcome["report"]}, ensure_ascii=False))
    return 0 if str(outcome["status"]).endswith("NOT_ACTIVATABLE") else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=_DEFAULT_OUTPUT_DIR)
    parser.add_argument("--catalog", type=Path, default=_DEFAULT_CATALOG)
    parser.add_argument("--file", type=Path, default=_DEFAULT_FILE)
    parser.add_argument("--source-row-limit", type=int, default=5000)
    parser.add_argument("--db-host", default=os.getenv("PUDDINGCLAW_CANONICAL_DB_HOST", "127.0.0.1"))
    parser.add_argument("--db-port", type=int, default=int(os.getenv("PUDDINGCLAW_CANONICAL_DB_PORT", "5432")))
    parser.add_argument("--db-name", default=os.getenv("PUDDINGCLAW_CANONICAL_DB_NAME", "insight_data"))
    parser.add_argument("--db-user", default=os.getenv("PUDDINGCLAW_CANONICAL_DB_USER", "pet"))
    parser.add_argument(
        "--canonical-collision-policy",
        type=Path,
        help="JSON mapping normalized_key -> selected candidate sha256; required to resolve canonical collisions",
    )
    return asyncio.run(_async_main(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
