from __future__ import annotations

import json

from scripts.phase8_local_ordered_cutover_shadow import run_shadow


def test_ordered_cutover_shadow_enforces_order_and_restart_rollback(tmp_path) -> None:
    result = run_shadow(output_dir=tmp_path)

    assert result["status"] == "PHASE8_LOCAL_ORDERED_CUTOVER_SHADOW_PASS_NOT_ACTIVATABLE"
    assert result["order_guarded"] is True
    assert result["database_dispatch"]["route"] == "platform"
    assert result["states_after_restart"] == {
        "document": "stable",
        "wiki": "stable",
        "table": "rolled_back",
        "database": "rolled_back",
    }


def test_ordered_cutover_shadow_report_is_bounded(tmp_path) -> None:
    run_shadow(output_dir=tmp_path)
    report = json.loads((tmp_path / "phase8-local-ordered-cutover-shadow-report.json").read_text())

    assert report["activation"] == "not-activated"
    assert "controlled local table rollback" not in json.dumps(report)
    assert "bounded-local-database-request" not in json.dumps(report)
