from __future__ import annotations

import json

from scripts.phase8_local_provider_handle_shadow import run_shadow


def test_local_provider_handle_shadow_blocks_without_vector_provider(tmp_path) -> None:
    catalog = tmp_path / "catalog.sqlite3"
    catalog.write_bytes(b"catalog")
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "page.md").write_text("page", encoding="utf-8")
    result = run_shadow(
        catalog=catalog,
        wiki_root=wiki,
        vector_uri="http://127.0.0.1:1",
        output_dir=tmp_path / "report",
    )

    assert result["status"] == "PHASE8_LOCAL_PROVIDER_HANDLE_SHADOW_BLOCKED_PROVIDER"
    assert result["activation_allowed"] is False
    assert result["all_required_observed"] is False


def test_local_provider_handle_shadow_report_has_no_path_or_uri(tmp_path) -> None:
    catalog = tmp_path / "catalog.sqlite3"
    catalog.write_bytes(b"catalog")
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    result = run_shadow(catalog=catalog, wiki_root=wiki, vector_uri="http://127.0.0.1:1", output_dir=tmp_path / "report")
    report = json.loads((tmp_path / "report/phase8-local-provider-handle-shadow-report.json").read_text())

    assert report["status"] == result["status"]
    assert str(tmp_path) not in json.dumps(report)
    assert "127.0.0.1" not in json.dumps(report)
