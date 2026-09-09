"""Replay the Platform Catalog query contract against the local staging copy.

This is an application-level shadow check, not runtime activation.  It reads
the independently staged Platform database through SQLite ``mode=ro`` and
records only stable identifiers, counts, and digests in its report.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from knowledge_contracts import Correlation, Principal
from knowledge_platform.catalog import CatalogQueryService, SqliteCatalogQueryRepository
from scripts.phase1_local_catalog_shadow import _database_fingerprint, _read_only_table_counts

_DEFAULT_OUTPUT_DIR = Path("artifacts/phase0b-local-catalog")


def _digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _bytes_digest(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _summary(result: Any) -> dict[str, Any]:
    payload = result.to_dict()
    summary: dict[str, Any] = {
        "status": payload["status"],
        "trace_id": payload["trace_id"],
    }
    if payload["status"] == "error":
        summary["error_code"] = payload["error"]["code"]
        return summary
    data = payload["data"]
    summary["data_keys"] = sorted(data)
    summary["data_digest"] = _digest(data)
    summary["evidence_count"] = len(payload["evidence"])
    summary["provenance_capability"] = payload["provenance"]["capability"]
    summary["provenance_space_id"] = payload["provenance"]["space_id"]
    summary["provenance_catalog_revision"] = payload["provenance"].get("catalog_revision")
    return summary


def _stage_binding(output_dir: Path, database_path: Path) -> tuple[bool, str, str]:
    report_path = output_dir / "local-catalog-stage-report.json"
    try:
        report_bytes = report_path.read_bytes()
        report = json.loads(report_bytes)
        target = report["targets"]["platform"]
        expected_files = target["files"]
        actual_files = _database_fingerprint(database_path)
        expected_path = str(Path(str(target["path"])).expanduser().resolve())
        if (
            report["format"] != "agent-knowledge-platform-local-catalog-stage/v1"
            or report["status"] not in {"STAGED_LOCAL_DATA", "STAGED_LOCAL_DATA_FILE_REACHABILITY_BLOCKED"}
            or report["activation"] != "not-activated"
            or expected_path != str(database_path.resolve())
            or target["bytes"] != database_path.stat().st_size
            or target["sha256"] != actual_files[database_path.name]["sha256"]
            or expected_files != actual_files
            or target["table_counts"] != _read_only_table_counts(database_path)
        ):
            return False, "stage report does not bind the current Platform target", _bytes_digest(report_bytes)
        return True, "", _bytes_digest(report_bytes)
    except (KeyError, OSError, TypeError, ValueError, sqlite3.Error):
        return False, "stage report is missing, unreadable, or invalid", ""


def run_query_shadow(*, output_dir: Path = _DEFAULT_OUTPUT_DIR) -> dict[str, Any]:
    output_dir = output_dir.expanduser().resolve()
    database_path = output_dir / "knowledge-platform.sqlite3"
    stage_bound, stage_error, stage_report_digest = _stage_binding(output_dir, database_path)
    before = _database_fingerprint(database_path)
    repository = SqliteCatalogQueryRepository(database_path)
    service = CatalogQueryService(repository)
    principal = Principal(subject_id="phase1-shadow", scopes=("knowledge.list", "knowledge.search", "knowledge.read"))
    correlation = Correlation(trace_id="phase1-catalog-shadow", request_id="phase1-catalog-shadow")

    collections = service.list_collections(principal=principal, correlation=correlation)
    collection_rows = collections.data.get("collections", []) if collections.status == "ok" else []
    # Use the first collection's declared Asset identity as the deterministic
    # fixture selector; the service itself remains the only query boundary.
    asset_id = ""
    if collection_rows:
        asset_ids = collection_rows[0].get("asset_ids", [])
        if isinstance(asset_ids, (list, tuple)) and asset_ids:
            asset_id = str(asset_ids[0])
    asset = service.read_asset(principal=principal, correlation=correlation, asset_id=asset_id) if asset_id else None
    asset_row = asset.data.get("asset", {}) if asset is not None and asset.status == "ok" else {}
    title = str(asset_row.get("title") or "")
    search = service.search_assets(
        principal=principal,
        correlation=correlation,
        text=title,
        limit=20,
    ) if title else None
    denied = service.read_asset(
        principal=Principal(subject_id="phase1-shadow-denied", scopes=("knowledge.list",)),
        correlation=correlation,
        asset_id=asset_id or "missing",
    )
    after = _database_fingerprint(database_path)
    stage_bound_after, stage_error_after, stage_report_digest_after = _stage_binding(output_dir, database_path)
    if not stage_bound_after:
        stage_error = stage_error_after

    search_rows = search.data.get("assets", []) if search is not None and search.status == "ok" else []
    search_ids = [str(row.get("id")) for row in search_rows]
    healthy = stage_bound and stage_bound_after and stage_report_digest == stage_report_digest_after and (
        collections.status == "ok"
        and int(collections.data.get("count", 0)) > 0
        and asset is not None
        and asset.status == "ok"
        and len(asset.evidence) == 1
        and search is not None
        and search.status == "ok"
        and asset_id in search_ids
        and len(search.evidence) >= 1
        and any(item.asset_id == asset_id and item.matched_by for item in search.evidence)
        and denied.status == "error"
        and denied.error is not None
        and denied.error.code.value == "permission_denied"
        and before == after
    )
    result = {
        "format": "agent-knowledge-platform-catalog-query-shadow/v1",
        "status": "PHASE1_QUERY_SHADOW_PASS_NOT_ACTIVATABLE" if healthy else "PHASE1_QUERY_SHADOW_FAILED",
        "activation": "not-activated",
        "scope": "read-only staged local Catalog application query boundary",
        "stage_report_digest": stage_report_digest,
        "stage_report_digest_after": stage_report_digest_after,
        "stage_binding_error": stage_error,
        "database": {
            "target": database_path.name,
            "read_mode": "sqlite-mode-ro",
            "files_before": before,
            "files_after": after,
            "database_unchanged": before == after,
        },
        "queries": {
            "list_collections": _summary(collections),
            "search_assets": _summary(search) if search is not None else {"status": "not-run"},
            "read_asset": _summary(asset) if asset is not None else {"status": "not-run"},
            "permission_denied": _summary(denied),
        },
        "observed": {
            "collection_count": int(collections.data.get("count", 0)) if collections.status == "ok" else 0,
            "asset_id": asset_id,
            "search_match_count": len(search_ids),
            "search_match_ids_digest": _digest(search_ids),
        },
    }
    report_path = output_dir / "catalog-query-shadow-report.json"
    report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=_DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    result = run_query_shadow(output_dir=args.output_dir)
    print(
        json.dumps(
            {
                "status": result["status"],
                "collection_count": result["observed"]["collection_count"],
                "search_match_count": result["observed"]["search_match_count"],
                "database_unchanged": result["database"]["database_unchanged"],
                "report": str(args.output_dir / "catalog-query-shadow-report.json"),
            },
            ensure_ascii=False,
        )
    )
    return 0 if result["status"] == "PHASE1_QUERY_SHADOW_PASS_NOT_ACTIVATABLE" else 1


if __name__ == "__main__":
    raise SystemExit(main())
