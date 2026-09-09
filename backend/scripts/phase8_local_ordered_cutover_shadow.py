"""Replay the ordered Phase 8 Capability cutover locally.

The rehearsal uses temporary SQLite control state and no production request or
Catalog.  It proves ordering, the atomic database query unit, rollback of a
unit and restart persistence of the rollback fence.
"""

from __future__ import annotations

import argparse
import json
import tempfile
from datetime import timedelta
from pathlib import Path
from typing import Any

from knowledge_platform.continuity import (
    CapabilityCutoverCoordinator,
    CapabilityTrafficController,
    SqliteCutoverStateStore,
    SqliteTrafficPolicyStore,
)
from knowledge_platform.continuity.traffic import TrafficPolicyError

_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_OUTPUT_DIR = _ROOT / "artifacts/phase0b-local-catalog"


def _coordinator(state_dir: Path) -> CapabilityCutoverCoordinator:
    traffic_store = SqliteTrafficPolicyStore(state_dir / "traffic.sqlite3")
    traffic = CapabilityTrafficController(store=traffic_store)
    return CapabilityCutoverCoordinator(
        traffic=traffic,
        store=SqliteCutoverStateStore(state_dir / "cutover.sqlite3"),
    )


def _promote(coordinator: CapabilityCutoverCoordinator, unit: str) -> None:
    coordinator.promote(
        unit=unit,
        deployment_revision="platform-local-phase8-v1",
        traffic_percent=100,
        rollback_window=timedelta(minutes=5),
    )


def run_shadow(*, output_dir: Path = _DEFAULT_OUTPUT_DIR) -> dict[str, Any]:
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {
        "format": "agent-knowledge-platform-phase8-local-ordered-cutover-shadow/v1",
        "activation": "not-activated",
        "status": "PHASE8_LOCAL_ORDERED_CUTOVER_SHADOW_FAILED",
        "order_guarded": False,
        "database_dispatch": None,
        "states_before_rollback": {},
        "states_after_restart": {},
    }
    try:
        with tempfile.TemporaryDirectory(prefix="phase8-ordered-cutover-state-") as temp_dir:
            state_dir = Path(temp_dir)
            coordinator = _coordinator(state_dir)
            _promote(coordinator, "document")
            coordinator.stabilize(unit="document", health_proof="document-health")
            try:
                _promote(coordinator, "table")
            except TrafficPolicyError:
                result["order_guarded"] = True
            _promote(coordinator, "wiki")
            coordinator.stabilize(unit="wiki", health_proof="wiki-health")
            _promote(coordinator, "table")
            coordinator.stabilize(unit="table", health_proof="table-health")
            _promote(coordinator, "database")
            database_result = coordinator.dispatch(
                capability="database_nl2sql_execute_readonly",
                routing_key="phase8-database-request",
                deployment_revision="platform-local-phase8-v1",
                request="bounded-local-database-request",
                platform_handler=lambda _request: "platform-database-result",
                legacy_handler=lambda _request: "legacy-database-result",
            )
            result["database_dispatch"] = {
                "route": database_result.decision.route,
                "fallback": database_result.fallback,
                "result": database_result.result,
            }
            result["states_before_rollback"] = {
                unit: coordinator.state(unit).status for unit in ("document", "wiki", "table", "database")
            }
            coordinator.rollback(unit="table", reason="controlled local table rollback")
            restarted = _coordinator(state_dir)
            result["states_after_restart"] = {
                unit: restarted.state(unit).status for unit in ("document", "wiki", "table", "database")
            }
            if (
                result["order_guarded"]
                and result["database_dispatch"] == {"route": "platform", "fallback": False, "result": "platform-database-result"}
                and result["states_before_rollback"] == {"document": "stable", "wiki": "stable", "table": "stable", "database": "active"}
                and result["states_after_restart"] == {"document": "stable", "wiki": "stable", "table": "rolled_back", "database": "rolled_back"}
            ):
                result["status"] = "PHASE8_LOCAL_ORDERED_CUTOVER_SHADOW_PASS_NOT_ACTIVATABLE"
    except (OSError, TrafficPolicyError, RuntimeError, ValueError, TypeError):
        result["error_type"] = "local_ordered_cutover_rehearsal_failed"
    report_path = output_dir / "phase8-local-ordered-cutover-shadow-report.json"
    report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    result["report"] = str(report_path)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=_DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    result = run_shadow(output_dir=args.output_dir)
    print(json.dumps({"status": result["status"], "report": result["report"]}, ensure_ascii=False))
    return 0 if str(result["status"]).endswith("NOT_ACTIVATABLE") else 1


if __name__ == "__main__":
    raise SystemExit(main())
