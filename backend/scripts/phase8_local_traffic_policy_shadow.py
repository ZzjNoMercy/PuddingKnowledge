"""Replay Phase 8 per-Capability traffic and rollback policy locally.

This is a deterministic policy rehearsal only.  It does not route production
requests, start a worker, or change a Catalog; all outcomes are summarized in
a bounded report.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from knowledge_platform.continuity import (
    CONTINUITY_CAPABILITIES,
    CapabilityTrafficController,
    CapabilityTrafficDispatcher,
    SqliteTrafficPolicyStore,
)

_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_OUTPUT_DIR = _ROOT / "artifacts/phase0b-local-catalog"


def run_shadow(*, output_dir: Path = _DEFAULT_OUTPUT_DIR) -> dict[str, Any]:
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    now = [datetime(2026, 9, 4, tzinfo=UTC)]
    with TemporaryDirectory(prefix="phase8-traffic-policy-state-") as state_dir:
        store = SqliteTrafficPolicyStore(Path(state_dir) / "traffic.sqlite3")
        active_revisions = {"platform-local-phase8-v1"}
        controller = CapabilityTrafficController(
            clock=lambda: now[0],
            store=store,
            active_revision_check=lambda revision: revision in active_revisions,
        )
        revision = "platform-local-phase8-v1"
        for capability in CONTINUITY_CAPABILITIES:
            controller.configure(
                capability=capability,
                deployment_revision=revision,
                enabled=True,
                traffic_percent=100,
                rollback_window=timedelta(minutes=5),
            )

        initial = {
            capability: controller.decide(
                capability=capability,
                routing_key=f"phase8-{capability}",
                deployment_revision=revision,
            )
            for capability in CONTINUITY_CAPABILITIES
        }
        dispatcher = CapabilityTrafficDispatcher(controller)
        dispatch_initial = dispatcher.dispatch(
            capability="wiki_compile",
            routing_key="phase8-dispatch-initial",
            deployment_revision=revision,
            request="bounded-local-request",
            platform_handler=lambda _request: "platform-result",
            legacy_handler=lambda _request: "legacy-result",
        )

        def failing_platform(_request: str) -> str:
            raise RuntimeError("controlled local failure")

        dispatch_failure = dispatcher.dispatch(
            capability="wiki_compile",
            routing_key="phase8-dispatch-failure",
            deployment_revision=revision,
            request="bounded-local-request",
            platform_handler=failing_platform,
            legacy_handler=lambda _request: "legacy-result",
        )
        after_failure = {
            capability: controller.decide(
                capability=capability,
                routing_key=f"phase8-{capability}",
                deployment_revision=revision,
            )
            for capability in CONTINUITY_CAPABILITIES
        }
        restarted = CapabilityTrafficController(
            clock=lambda: now[0],
            store=SqliteTrafficPolicyStore(Path(state_dir) / "traffic.sqlite3"),
            active_revision_check=lambda revision: revision in active_revisions,
        )
        after_restart = restarted.decide(
            capability="wiki_compile",
            routing_key="phase8-restart",
            deployment_revision=revision,
        )
        active_revisions.remove(revision)
        after_deployment_rollback = restarted.decide(
            capability="wiki_compile",
            routing_key="phase8-deployment-rollback",
            deployment_revision=revision,
        )
        active_revisions.add(revision)
        now[0] += timedelta(minutes=5)
        after_expiry = {
            capability: restarted.decide(
                capability=capability,
                routing_key=f"phase8-{capability}",
                deployment_revision=revision,
            )
            for capability in CONTINUITY_CAPABILITIES
        }

    result: dict[str, Any] = {
        "format": "agent-knowledge-platform-phase8-local-traffic-policy-shadow/v1",
        "activation": "not-activated",
        "status": "PHASE8_LOCAL_TRAFFIC_POLICY_SHADOW_FAILED",
        "deployment_revision": revision,
        "dispatcher": {
            "initial_result": dispatch_initial.result,
            "failure_fallback": dispatch_failure.fallback,
            "failure_result": dispatch_failure.result,
            "failure_reason": dispatch_failure.decision.reason,
        },
        "durable_restart": {
            "failure_route": after_restart.route,
            "failure_reason": after_restart.reason,
        },
        "deployment_revision_fence": {
            "inactive_route": after_deployment_rollback.route,
            "inactive_reason": after_deployment_rollback.reason,
        },
        "capabilities": {},
        "audit_event_types": [event.event_type for event in controller.events],
    }
    for capability in CONTINUITY_CAPABILITIES:
        result["capabilities"][capability] = {
            "initial_route": initial[capability].route,
            "failure_route": after_failure[capability].route,
            "failure_reason": after_failure[capability].reason,
            "expired_route": after_expiry[capability].route,
            "expired_reason": after_expiry[capability].reason,
        }
    if (
        all(decision.route == "platform" for decision in initial.values())
        and after_failure["wiki_compile"].route == "legacy"
        and after_failure["wiki_compile"].reason == "unhealthy"
        and after_restart.route == "legacy"
        and after_restart.reason == "unhealthy"
        and after_deployment_rollback.route == "legacy"
        and after_deployment_rollback.reason == "deployment_not_active"
        and all(after_failure[capability].route == "platform" for capability in CONTINUITY_CAPABILITIES if capability != "wiki_compile")
        and all(after_expiry[capability].route == "legacy" for capability in CONTINUITY_CAPABILITIES)
    ):
        result["status"] = "PHASE8_LOCAL_TRAFFIC_POLICY_SHADOW_PASS_NOT_ACTIVATABLE"
    report_path = output_dir / "phase8-local-traffic-policy-shadow-report.json"
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
