"""Exercise Space-scoped Platform notifications against a local Catalog copy.

The canonical staged Catalog is never changed. Existing migrated events without
an explicit ownership binding remain invisible; a synthetic event is published
only in the temporary copy with an explicit Space binding, then read through
the real Admin/FastAPI edge.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from knowledge_contracts import NotificationEvent, Principal
from knowledge_platform.catalog import migrate_to_latest
from knowledge_platform.catalog.notification_scope import SqliteNotificationEventScopeStore
from knowledge_platform.catalog.notification_service import NotificationEventQueryService
from knowledge_platform.catalog.sqlite_query import SqliteCatalogQueryRepository
from scripts.phase8_local_platform_http_shadow import _build_app

_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_CATALOG = _ROOT / "artifacts/phase0b-local-catalog/knowledge-platform.sqlite3"
_DEFAULT_OUTPUT_DIR = _ROOT / "artifacts/phase0b-local-catalog"
_SPACE_ID = "space_kb_default"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _write_report(output_dir: Path, report: dict[str, object]) -> dict[str, object]:
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / "phase9-local-notification-shadow-report.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report["report"] = str(target)
    return report


def run_shadow(
    *,
    catalog: Path = _DEFAULT_CATALOG,
    output_dir: Path = _DEFAULT_OUTPUT_DIR,
) -> dict[str, object]:
    catalog = catalog.expanduser().absolute()
    output_dir = output_dir.expanduser().absolute()
    canonical_before = _sha256(catalog)
    result: dict[str, object] = {
        "format": "agent-knowledge-platform-phase9-local-notification-shadow/v1",
        "status": "PHASE9_LOCAL_NOTIFICATION_SHADOW_FAILED",
        "activation": "not-activated",
        "canonical_catalog_before": canonical_before,
        "canonical_catalog_after": None,
        "legacy_unbound_event_count": 0,
        "bound_event_count": 0,
        "http": None,
    }
    try:
        with tempfile.TemporaryDirectory(prefix="phase9-local-notification-shadow-") as temp_dir:
            temporary_catalog = Path(temp_dir) / "knowledge-platform.sqlite3"
            shutil.copy2(catalog, temporary_catalog)
            engine = create_engine(f"sqlite:///{temporary_catalog}")
            with engine.begin() as connection:
                migrate_to_latest(connection)
                legacy_unbound = connection.execute(
                    text(
                        "SELECT COUNT(*) FROM knowledge_notification_events AS e "
                        "LEFT JOIN knowledge_notification_event_scopes AS s ON s.event_id = e.id "
                        "WHERE s.event_id IS NULL"
                    )
                ).scalar_one()
            engine.dispose()
            store = SqliteNotificationEventScopeStore(temporary_catalog)
            store.publish(
                event=NotificationEvent(
                    event_id="notification_phase9_shadow_1",
                    event_type="platform.notification.v1",
                    subject_type="processing_job",
                    subject_id="processing_shadow_1",
                    title="Local Platform notification shadow",
                    body="Explicitly Space-bound local shadow event.",
                    payload={"status": "published"},
                    occurred_at="2026-09-05T00:00:00Z",
                ),
                category="processing",
                space_id=_SPACE_ID,
            )
            repository = SqliteCatalogQueryRepository(temporary_catalog)
            principal = Principal(
                subject_id="phase9-local-notification-shadow",
                scopes=("knowledge.admin", f"knowledge.space:{_SPACE_ID}"),
            )
            app = _build_app(
                repository,
                {},
                principal,
                notifications=NotificationEventQueryService(store),
            )
            with TestClient(app) as client:
                response = client.get(f"/v1/notifications?space_id={_SPACE_ID}&limit=10")
            payload = response.json()
            items = payload.get("data", {}).get("notifications", []) if isinstance(payload, dict) else []
            result["legacy_unbound_event_count"] = int(legacy_unbound)
            result["bound_event_count"] = len(items)
            result["http"] = {
                "status_code": response.status_code,
                "status": payload.get("status") if isinstance(payload, dict) else "invalid",
                "count": len(items),
            }
            if response.status_code == 200 and payload.get("status") == "ok" and len(items) == 1:
                result["status"] = "PHASE9_LOCAL_NOTIFICATION_SHADOW_PASS_NOT_ACTIVATABLE"
    except Exception as error:  # reports must not expose lower-layer details
        result["error_type"] = type(error).__name__
    result["canonical_catalog_after"] = _sha256(catalog)
    result["canonical_catalog_unchanged"] = result["canonical_catalog_before"] == result["canonical_catalog_after"]
    return _write_report(output_dir, result)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog", type=Path, default=_DEFAULT_CATALOG)
    parser.add_argument("--output-dir", type=Path, default=_DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    report = run_shadow(catalog=args.catalog, output_dir=args.output_dir)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["status"] == "PHASE9_LOCAL_NOTIFICATION_SHADOW_PASS_NOT_ACTIVATABLE" else 1


if __name__ == "__main__":
    raise SystemExit(main())
