"""Produce Knowledge target-domain inventories from a verified candidate tree."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import sqlite3
from urllib.parse import quote

from . import cutover_readiness as readiness


FORMAT = "puddingknowledge-cutover-domain-inventory/v1"
DOMAINS = ("knowledge_catalog", "connector_jobs")
CONNECTOR_JOB_TABLES = (
    "knowledge_connectors",
    "knowledge_database_connectors",
    "knowledge_source_items",
    "knowledge_sync_runs",
    "knowledge_web_captures",
    "knowledge_ingestion_jobs",
    "knowledge_ingestion_events",
    "knowledge_structured_assets",
    "knowledge_query_results",
    "knowledge_query_result_scopes",
    "knowledge_processing_jobs",
    "knowledge_processing_events",
    "knowledge_authoring_jobs",
    "knowledge_authoring_events",
    "knowledge_notification_events",
    "knowledge_notification_event_scopes",
    "knowledge_collection_bindings",
)
MAX_ROWS = 1_000_000


class DomainInventoryError(ValueError):
    pass


def _row_value(value):
    if value is None or isinstance(value, (str, int)) or type(value) is bool:
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise DomainInventoryError("candidate Catalog row contains a non-finite number")
        return value
    if isinstance(value, bytes):
        return {"bytes_sha256": hashlib.sha256(value).hexdigest(), "size": len(value)}
    raise DomainInventoryError("candidate Catalog row contains an unsupported SQLite value")


def _connector_ids(catalog: Path) -> list[str]:
    raw_before = readiness._read_private(catalog, readiness._MAX_CANDIDATE_FILE)
    connection = sqlite3.connect(f"file:{quote(str(catalog), safe='/')}?mode=ro&immutable=1", uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise DomainInventoryError("candidate Catalog failed integrity validation")
        available = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        result = []
        for table in CONNECTOR_JOB_TABLES:
            if table not in available:
                continue
            columns = [str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')]
            if not columns:
                raise DomainInventoryError("candidate Catalog table has no columns")
            quoted = ",".join('"' + column.replace('"', '""') + '"' for column in columns)
            for row in connection.execute(f'SELECT {quoted} FROM "{table}"'):
                material = readiness._encode({"table": table, "row": {column: _row_value(value) for column, value in zip(columns, row)}})
                result.append("row:" + hashlib.sha256(material).hexdigest())
                if len(result) > MAX_ROWS:
                    raise DomainInventoryError("candidate Catalog inventory exceeds its row budget")
    finally:
        connection.close()
    if readiness._read_private(catalog, readiness._MAX_CANDIDATE_FILE) != raw_before:
        raise DomainInventoryError("candidate Catalog changed during inventory production")
    if len(result) != len(set(result)):
        raise DomainInventoryError("candidate Catalog contains duplicate canonical connector/job rows")
    return sorted(result)


def produce_inventory(*, migration_receipt: Path | str, candidate: Path | str,
                      domain: str, output: Path | str) -> dict:
    if domain not in DOMAINS:
        raise DomainInventoryError("unknown Knowledge cutover domain")
    migration_raw = readiness._read_private(migration_receipt)
    migration = readiness._object(migration_raw, "migration receipt")
    manifest, _manifest_digest, tree_digest = readiness._verify_candidate(candidate, migration)
    root = readiness._path(Path(candidate).expanduser())
    if domain == "knowledge_catalog":
        inventory = sorted(f"asset:{asset_id}" for asset_id in manifest["asset_bindings"])
    else:
        inventory = _connector_ids(root / "catalog.sqlite3")
    if any(not readiness._TOKEN.fullmatch(item) for item in inventory):
        raise DomainInventoryError("target inventory contains a non-portable identity")
    receipt = {"format": FORMAT, "producer": "puddingknowledge", "target_artifact_sha256": tree_digest,
               "inventory": inventory, "inventory_sha256": readiness._digest(readiness._encode(inventory))}
    readiness._publish(Path(output), readiness._encode(receipt))
    return receipt


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--migration-receipt", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--domain", choices=DOMAINS, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = produce_inventory(migration_receipt=args.migration_receipt, candidate=args.candidate,
                                   domain=args.domain, output=args.output)
    except Exception:
        print(json.dumps({"format": FORMAT, "status": "error", "error_code": "domain_inventory_rejected"}, sort_keys=True))
        return 1
    print(json.dumps({"format": FORMAT, "domain": args.domain, "inventory_sha256": result["inventory_sha256"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
