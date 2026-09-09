from __future__ import annotations

import json

from scripts.phase8_local_catalog_activation_shadow import run_shadow


def test_local_catalog_activation_shadow_is_not_activatable(tmp_path) -> None:
    catalog = tmp_path / "canonical.sqlite3"
    catalog.write_bytes(b"local-catalog")
    result = run_shadow(catalog=catalog, output_dir=tmp_path / "report")

    assert result["status"] == "PHASE8_LOCAL_CATALOG_ACTIVATION_SHADOW_PASS_NOT_ACTIVATABLE"
    assert result["source_unchanged"] is True
    assert result["rollback"]["active_revision"] == "legacy-local-v1"


def test_local_catalog_activation_shadow_report_is_bounded(tmp_path) -> None:
    catalog = tmp_path / "canonical.sqlite3"
    catalog.write_bytes(b"local-catalog")
    result = run_shadow(catalog=catalog, output_dir=tmp_path / "report")
    report = json.loads((tmp_path / "report/phase8-local-catalog-activation-shadow-report.json").read_text())

    assert report["status"] == result["status"]
    assert "controlled local activation failure" not in json.dumps(report)
