from __future__ import annotations

import json

from scripts.phase8_local_traffic_policy_shadow import run_shadow


def test_traffic_policy_shadow_rehearses_independent_cutback_and_expiry(tmp_path) -> None:
    result = run_shadow(output_dir=tmp_path)

    assert result["status"] == "PHASE8_LOCAL_TRAFFIC_POLICY_SHADOW_PASS_NOT_ACTIVATABLE"
    assert result["dispatcher"] == {
        "initial_result": "platform-result",
        "failure_fallback": True,
        "failure_result": "legacy-result",
        "failure_reason": "unhealthy",
    }
    assert result["durable_restart"] == {"failure_route": "legacy", "failure_reason": "unhealthy"}
    assert result["capabilities"]["wiki_compile"] == {
        "initial_route": "platform",
        "failure_route": "legacy",
        "failure_reason": "unhealthy",
        "expired_route": "legacy",
        "expired_reason": "unhealthy",
    }
    assert result["capabilities"]["capture_processing"]["failure_route"] == "platform"
    assert result["capabilities"]["capture_processing"]["expired_reason"] == "rollback_window_expired"


def test_traffic_policy_shadow_report_does_not_store_failure_detail(tmp_path) -> None:
    result = run_shadow(output_dir=tmp_path)
    report = json.loads((tmp_path / "phase8-local-traffic-policy-shadow-report.json").read_text())

    assert report["status"] == result["status"]
    assert "controlled local failure" not in json.dumps(report)
