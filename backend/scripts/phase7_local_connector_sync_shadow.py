"""Run a non-activating local Connector Sync rehearsal."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from knowledge_platform.connector_sync import (
    ConnectorSyncRequest,
    ConnectorSyncWorker,
    LocalConnectorSourceProvider,
    SqliteConnectorSyncStore,
)

_DEFAULT_OUTPUT_DIR = Path("artifacts/phase0b-local-catalog")


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _path_digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(str(path.expanduser().absolute()).encode()).hexdigest()


def _binding(catalog: Path, *, source_item_id: str, connector_id: str) -> dict[str, str] | None:
    with sqlite3.connect(catalog) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            """
            SELECT s.id AS source_item_id, s.space_id, s.connector_id, s.asset_id,
                   s.content_digest, a.revision
              FROM knowledge_source_items AS s
              JOIN knowledge_assets AS a ON a.id = s.asset_id
             WHERE s.id = ? AND s.connector_id = ? AND s.status = 'ready'
            """,
            (source_item_id, connector_id),
        ).fetchone()
    return dict(row) if row is not None else None


def run_shadow(
    *,
    output_dir: Path = _DEFAULT_OUTPUT_DIR,
    connector_id: str = "",
    source_item_id: str = "",
    source_path: Path | None = None,
) -> dict[str, Any]:
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    catalog = output_dir / "knowledge-platform.sqlite3"
    result: dict[str, Any] = {
        "format": "agent-knowledge-platform-phase7-local-connector-sync-shadow/v1",
        "activation": "not-activated",
        "status": "PHASE7_CONNECTOR_SYNC_SHADOW_REJECTED_NO_EXPLICIT_SOURCE_BINDING",
        "catalog_path_digest": _path_digest(catalog),
        "connector_id": connector_id or None,
        "source_item_id": source_item_id or None,
        "source_path_digest": _path_digest(source_path) if source_path else None,
        "sync": None,
        "canonical_catalog_unchanged": None,
    }
    try:
        if not connector_id or not source_item_id or source_path is None:
            return _write_report(output_dir, result)
        before_catalog_digest = _digest(catalog)
        binding = _binding(catalog, source_item_id=source_item_id, connector_id=connector_id)
        if binding is None:
            result["status"] = "PHASE7_CONNECTOR_SYNC_SHADOW_REJECTED_BINDING_NOT_READY"
            return _write_report(output_dir, result)
        actual_digest = _digest(source_path)
        if actual_digest != binding["content_digest"]:
            result["status"] = "PHASE7_CONNECTOR_SYNC_SHADOW_REJECTED_DIGEST_MISMATCH"
            return _write_report(output_dir, result)
        with tempfile.TemporaryDirectory(prefix="phase7-connector-sync-shadow-") as temp_dir:
            temporary_catalog = Path(temp_dir) / "knowledge-platform.sqlite3"
            shutil.copy2(catalog, temporary_catalog)
            worker = ConnectorSyncWorker(
                source=LocalConnectorSourceProvider(),
                store=SqliteConnectorSyncStore(database_path=temporary_catalog),
            )
            request = ConnectorSyncRequest(
                connector_id=connector_id,
                space_id=binding["space_id"],
                source_paths={source_item_id: source_path.expanduser().absolute()},
                idempotency_key=f"phase7-local-connector-{source_item_id}-{actual_digest.removeprefix('sha256:')[:24]}",
            )
            first = worker.sync(request)
            second = worker.sync(request)
            if first != second or first.discovered != 1:
                raise RuntimeError("Connector Sync idempotency rehearsal failed")
            result["sync"] = {
                "run_id": first.run_id,
                "discovered": first.discovered,
                "changed": first.changed,
                "unchanged": first.unchanged,
                "second_matches_first": first == second,
                "source_digest": actual_digest,
            }
            with sqlite3.connect(temporary_catalog) as connection:
                row = connection.execute(
                    "SELECT status, current_step, progress, lease_owner, stats_json "
                    "FROM knowledge_sync_runs WHERE id = ?",
                    (first.run_id,),
                ).fetchone()
                if row is None or row[0:3] != ("succeeded", "completed", 100) or row[3] is not None:
                    raise RuntimeError("Connector Sync terminal lease state is invalid")
        result["canonical_catalog_unchanged"] = _digest(catalog) == before_catalog_digest
        if not result["canonical_catalog_unchanged"]:
            raise RuntimeError("canonical Catalog changed during Connector Sync shadow")
        result["status"] = "PHASE7_CONNECTOR_SYNC_SHADOW_PASS_NOT_ACTIVATABLE"
    except (OSError, TypeError, ValueError, LookupError, RuntimeError, sqlite3.Error):
        result["status"] = "PHASE7_CONNECTOR_SYNC_SHADOW_FAILED"
        result["error"] = "phase7 local Connector Sync shadow failed"
    return _write_report(output_dir, result)


def _write_report(output_dir: Path, result: dict[str, Any]) -> dict[str, Any]:
    report_path = output_dir / "phase7-local-connector-sync-shadow-report.json"
    report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    result["report"] = str(report_path)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=_DEFAULT_OUTPUT_DIR)
    parser.add_argument("--connector-id", required=True)
    parser.add_argument("--source-item-id", required=True)
    parser.add_argument("--file", type=Path, required=True)
    args = parser.parse_args()
    result = run_shadow(
        output_dir=args.output_dir,
        connector_id=args.connector_id,
        source_item_id=args.source_item_id,
        source_path=args.file,
    )
    print(json.dumps({"status": result["status"], "report": result["report"]}, ensure_ascii=False))
    return 0 if str(result["status"]).endswith("NOT_ACTIVATABLE") or "REJECTED" in str(result["status"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
