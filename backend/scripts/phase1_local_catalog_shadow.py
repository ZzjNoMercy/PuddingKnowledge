"""Run a read-only smoke test against the staged local Catalog.

This command is deliberately narrower than Phase 1 activation.  It opens the
generated Platform and Harness databases with SQLite ``mode=ro``, verifies
their structural health, and prints the Collections and a bounded asset
summary.  It does not import the active runtime, repair file references, or
write to either Catalog.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import quote

_SECRET_VALUE = re.compile(r"(?:bearer|password|passphrase|secret|api[_-]?key|access[_-]?key|refresh[_-]?token)", re.I)
_DEFAULT_OUTPUT_DIR = Path("artifacts/phase0b-local-catalog")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _database_fingerprint(path: Path) -> dict[str, Any]:
    files: dict[str, Any] = {}
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        if candidate.is_file():
            files[candidate.name] = {"bytes": candidate.stat().st_size, "sha256": _sha256(candidate)}
    return files


def _table_counts(connection: sqlite3.Connection, tables: set[str]) -> dict[str, int]:
    return {name: _count(connection, name) for name in sorted(tables)}


def _read_only_table_counts(path: Path) -> dict[str, int]:
    connection = _read_only_connection(path)
    try:
        tables = _table_names(connection)
        return _table_counts(connection, tables)
    finally:
        connection.close()


def _read_only_connection(path: Path) -> sqlite3.Connection:
    if path.is_symlink() or not path.is_file():
        raise FileNotFoundError(f"staged Catalog database does not exist: {path}")
    uri = f"file:{quote(str(path.resolve()), safe='/')}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA query_only=ON")
    return connection


def _table_names(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return {str(row[0]) for row in rows}


def _count(connection: sqlite3.Connection, table: str) -> int:
    return int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])


def _json(value: Any, *, field: str) -> Any:
    if value in (None, ""):
        return []
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        raise RuntimeError(f"{field} contains invalid JSON") from None


def _safe_count_map(value: Any) -> tuple[dict[str, int], bool]:
    if not isinstance(value, dict):
        return {}, False
    if any(
        not isinstance(key, str)
        or not key.strip()
        or not isinstance(count, int)
        or isinstance(count, bool)
        or count < 0
        for key, count in value.items()
    ):
        return {}, False
    return {key: count for key, count in value.items()}, True


def _inspect_database(path: Path, *, role: str, asset_limit: int) -> dict[str, Any]:
    before = _database_fingerprint(path)
    connection = _read_only_connection(path)
    try:
        tables = _table_names(connection)
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = [dict(row) for row in connection.execute("PRAGMA foreign_key_check").fetchall()]
        query_only = int(connection.execute("PRAGMA query_only").fetchone()[0])
        table_counts = _table_counts(connection, tables)
        result: dict[str, Any] = {
            "role": role,
            "path": str(path.resolve()),
            "read_mode": "sqlite-mode-ro",
            "query_only": query_only,
            "integrity_check": integrity,
            "foreign_key_check": foreign_keys,
            "table_counts": table_counts,
        }
        if role == "platform":
            required = {"knowledge_spaces", "knowledge_assets", "knowledge_datasets"}
            missing = sorted(required - tables)
            if missing:
                raise RuntimeError(f"Platform Catalog is missing required tables: {missing}")
            spaces = [
                dict(row)
                for row in connection.execute(
                    "SELECT id, name FROM knowledge_spaces ORDER BY id"
                ).fetchall()
            ]
            space_ids = {str(space["id"]) for space in spaces}
            for space in spaces:
                space["name_digest"] = _digest(space.pop("name"))
            collections: list[dict[str, Any]] = []
            for row in connection.execute(
                "SELECT id, space_id, name, version, kind, capabilities, freshness, asset_ids, manifest_digest "
                "FROM knowledge_datasets ORDER BY space_id, id, version"
            ).fetchall():
                collection = dict(row)
                collection["capabilities"] = _json(collection["capabilities"], field="knowledge_datasets.capabilities")
                collection["freshness"] = _json(collection["freshness"], field="knowledge_datasets.freshness")
                collection["asset_ids"] = _json(collection["asset_ids"], field="knowledge_datasets.asset_ids")
                if (
                    not isinstance(collection["capabilities"], list)
                    or not isinstance(collection["freshness"], dict)
                    or not isinstance(collection["asset_ids"], list)
                ):
                    raise RuntimeError("Collection capabilities/ freshness/ asset_ids have an invalid JSON shape")
                if (
                    any(not isinstance(item, str) or not item for item in collection["capabilities"])
                    or any(not isinstance(item, str) or not item for item in collection["asset_ids"])
                    or len(collection["asset_ids"]) != len(set(collection["asset_ids"]))
                    or str(collection["space_id"]) not in space_ids
                    or set(collection["freshness"]) - {"mode", "source_revision"}
                    or not isinstance(collection["freshness"].get("mode"), str)
                    or not collection["freshness"].get("mode")
                    or not isinstance(collection["freshness"].get("source_revision"), str)
                ):
                    raise RuntimeError(f"Collection has an invalid identity or freshness shape: {row['id']}")
                collection["asset_count"] = len(collection["asset_ids"])
                collection["name_digest"] = _digest(collection.pop("name"))
                source_revision = collection["freshness"]["source_revision"]
                actual_manifest = {
                    "asset_ids": collection["asset_ids"],
                    "source_revision": source_revision,
                    "version": collection["version"],
                }
                if collection.pop("manifest_digest") != _digest(actual_manifest):
                    raise RuntimeError(f"Collection manifest digest mismatch: {collection['id']}")
                collection["freshness_mode"] = collection.pop("freshness").get("mode")
                collection["source_revision_digest"] = _digest(source_revision)
                collections.append(collection)
            asset_rows = connection.execute(
                "SELECT id, space_id, kind, title, mime_type, source_type, source_uri, revision, content_digest "
                "FROM knowledge_assets ORDER BY space_id, id"
            ).fetchall()
            asset_ids_by_space: dict[str, set[str]] = {}
            for row in asset_rows:
                if str(row["space_id"]) not in space_ids:
                    raise RuntimeError(f"Asset belongs to an unknown Space: {row['id']}")
                if not str(row["source_uri"]).startswith("knowledge://"):
                    raise RuntimeError(f"Asset has a non-stable source URI: {row['id']}")
                asset_ids_by_space.setdefault(str(row["space_id"]), set()).add(str(row["id"]))
            for collection in collections:
                missing_assets = sorted(
                    set(map(str, collection["asset_ids"])) - asset_ids_by_space.get(str(collection["space_id"]), set())
                )
                if missing_assets:
                    raise RuntimeError(f"Collection references missing Assets: {collection['id']}: {missing_assets}")
            assets = [
                {
                    "id": str(row["id"]),
                    "space_id": str(row["space_id"]),
                    "kind": str(row["kind"]),
                    "title_digest": _digest(row["title"]),
                    "mime_type": str(row["mime_type"]),
                    "source_type": str(row["source_type"]),
                    "source_uri_digest": _digest(row["source_uri"]),
                    "revision_digest": _digest(row["revision"]),
                    "content_digest_digest": _digest(row["content_digest"]),
                }
                for row in asset_rows[:asset_limit]
            ]
            result["collections"] = collections
            result["spaces"] = spaces
            result["assets"] = assets
            result["assets_returned"] = len(assets)
            result["assets_total"] = table_counts["knowledge_assets"]
        elif "worker_access_logs" in tables:
            findings: list[dict[str, Any]] = []
            columns = [str(row[1]) for row in connection.execute("PRAGMA table_info(worker_access_logs)").fetchall()]
            for row in connection.execute("SELECT * FROM worker_access_logs").fetchall():
                for column in columns:
                    value = row[column]
                    if isinstance(value, str) and _SECRET_VALUE.search(value) and value != "<redacted>":
                        findings.append({"column": column, "row_id": row[columns[0]], "reason": "secret-like value"})
            result["secret_like_findings"] = findings
    finally:
        connection.close()
    after = _database_fingerprint(path)
    result["database_unchanged"] = before == after
    result["files_before"] = before
    result["files_after"] = after
    return result


def run_shadow_smoke(*, output_dir: Path = _DEFAULT_OUTPUT_DIR, asset_limit: int = 20) -> dict[str, Any]:
    if asset_limit < 1:
        raise ValueError("asset_limit must be positive")
    output_dir = output_dir.expanduser().resolve()
    platform = _inspect_database(output_dir / "knowledge-platform.sqlite3", role="platform", asset_limit=asset_limit)
    harness = _inspect_database(output_dir / "harness.sqlite3", role="harness", asset_limit=asset_limit)
    stage_report_path = output_dir / "local-catalog-stage-report.json"
    stage_report_error = None
    if not stage_report_path.is_file():
        stage_report = {}
        stage_report_error = "stage report is missing"
    else:
        try:
            stage_report = json.loads(stage_report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            stage_report = {}
            stage_report_error = "stage report is unreadable or invalid JSON"
    verification = stage_report.get("physical_reference_verification", {}) if isinstance(stage_report, dict) else {}
    structural = stage_report.get("structural_copy_execution", {}) if isinstance(stage_report, dict) else {}
    missing_count = int(verification.get("missing_count", -1)) if isinstance(verification, dict) else -1
    stage_report_valid = (
        isinstance(stage_report, dict)
        and stage_report.get("format") == "agent-knowledge-platform-local-catalog-stage/v1"
        and isinstance(stage_report.get("generator"), dict)
        and stage_report["generator"].get("sha256")
        == _sha256(Path(__file__).with_name("phase0b_local_catalog_copy.py"))
        and stage_report.get("status") in {"STAGED_LOCAL_DATA", "STAGED_LOCAL_DATA_FILE_REACHABILITY_BLOCKED"}
        and stage_report.get("activation") == "not-activated"
        and isinstance(structural, dict)
        and structural.get("activation_state") == "NOT_ACTIVATABLE_STAGE"
        and isinstance(missing_count, int)
        and missing_count >= 0
        and stage_report_error is None
    )
    targets = stage_report.get("targets") if isinstance(stage_report, dict) else None
    if not isinstance(targets, dict):
        stage_report_valid = False
        stage_report_error = "stage report targets are missing or invalid"
        targets = {}
    for role, database in (("platform", platform), ("harness", harness)):
        target = targets.get(role, {})
        current_file = database["files_after"].get(database["path"].split("/")[-1], {})
        if (
            not isinstance(target, dict)
            or str(Path(str(target.get("path", ""))).expanduser().resolve()) != database["path"]
            or target.get("bytes") != current_file.get("bytes")
            or target.get("sha256") != current_file.get("sha256")
            or target.get("files") != database["files_after"]
            or target.get("table_counts") != database["table_counts"]
        ):
            stage_report_valid = False
            stage_report_error = f"{role} target does not match the stage report"
            break
    source = stage_report.get("source") if isinstance(stage_report, dict) else None
    if stage_report_valid and isinstance(source, dict):
        source_path = Path(str(source.get("path", ""))).expanduser()
        live_files = _database_fingerprint(source_path) if source_path.is_file() else {}
        if (
            source_path.is_symlink()
            or not source_path.is_file()
            or source.get("live_files") != live_files
            or source.get("table_counts") != _read_only_table_counts(source_path)
        ):
            stage_report_valid = False
            stage_report_error = "source Catalog does not match the stage report"
    elif stage_report_valid:
        stage_report_valid = False
        stage_report_error = "stage report source binding is missing or invalid"
    stage_status = stage_report.get("status", "stage-report-missing") if isinstance(stage_report, dict) else "invalid"
    raw_triage = stage_report.get("physical_reference_triage") if isinstance(stage_report, dict) else None
    stage_reference_triage: dict[str, dict[str, int]] = {}
    triage_valid = raw_triage is None
    if raw_triage is not None:
        triage_valid = isinstance(raw_triage, dict)
        if triage_valid:
            for key in ("resolution_counts", "content_digest_resolution_counts"):
                if key not in raw_triage:
                    continue
                safe_counts, counts_valid = _safe_count_map(raw_triage[key])
                if not counts_valid:
                    triage_valid = False
                    break
                stage_reference_triage[key] = safe_counts
    if not triage_valid:
        stage_report_valid = False
        stage_report_error = "stage report triage is invalid"
    healthy = all(
        database["query_only"] == 1
        and database["integrity_check"] == "ok"
        and not database["foreign_key_check"]
        and database["database_unchanged"]
        for database in (platform, harness)
    )
    healthy = healthy and not harness.get("secret_like_findings") and stage_report_valid
    result = {
        "format": "agent-knowledge-platform-local-catalog-shadow/v1",
        "status": "SHADOW_SMOKE_PASS_NOT_ACTIVATABLE" if healthy else "SHADOW_SMOKE_FAILED",
        "scope": "read-only staged local Catalog; no active runtime wiring",
        "activation": "not-activated",
        "stage_status": stage_status,
        "stage_missing_physical_references": missing_count,
        "stage_reference_triage": stage_reference_triage,
        "stage_report_valid": stage_report_valid,
        "stage_report_error": stage_report_error,
        "databases": {"platform": platform, "harness": harness},
    }
    report_path = output_dir / "local-catalog-shadow-report.json"
    report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=_DEFAULT_OUTPUT_DIR)
    parser.add_argument("--asset-limit", type=int, default=20)
    args = parser.parse_args()
    if args.asset_limit < 1:
        parser.error("--asset-limit must be positive")
    result = run_shadow_smoke(output_dir=args.output_dir, asset_limit=args.asset_limit)
    triage = result["stage_reference_triage"]
    print(
        json.dumps(
            {
                "status": result["status"],
                "stage_status": result["stage_status"],
                "collections": len(result["databases"]["platform"].get("collections", [])),
                "assets_returned": result["databases"]["platform"].get("assets_returned", 0),
                "assets_total": result["databases"]["platform"].get("assets_total", 0),
                "stage_missing_physical_references": result["stage_missing_physical_references"],
                "stage_reference_triage": triage,
                "report": str(args.output_dir / "local-catalog-shadow-report.json"),
            },
            ensure_ascii=False,
        )
    )
    return 0 if result["status"] == "SHADOW_SMOKE_PASS_NOT_ACTIVATABLE" else 1


if __name__ == "__main__":
    raise SystemExit(main())
