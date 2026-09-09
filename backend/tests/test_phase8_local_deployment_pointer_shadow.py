from __future__ import annotations

import json

from scripts.phase8_local_deployment_pointer_shadow import run_shadow


def test_local_deployment_pointer_shadow_is_not_activatable(tmp_path) -> None:
    catalog = tmp_path / "canonical.sqlite3"
    catalog.write_bytes(b"local-catalog")
    wiki_root = tmp_path / "wiki"
    wiki_root.mkdir()
    (wiki_root / "page.md").write_text("local wiki", encoding="utf-8")
    result = run_shadow(catalog=catalog, wiki_root=wiki_root, output_dir=tmp_path / "report")

    assert result["status"] == "PHASE8_LOCAL_DEPLOYMENT_POINTER_SHADOW_BLOCKED_PROVIDER"
    assert result["provider_handles"]
    assert result["activation"] == "not-activated"


def test_local_deployment_pointer_shadow_rehearses_with_explicit_vector_probe(tmp_path) -> None:
    catalog = tmp_path / "canonical.sqlite3"
    catalog.write_bytes(b"local-catalog")
    wiki_root = tmp_path / "wiki"
    wiki_root.mkdir()
    (wiki_root / "page.md").write_text("local wiki", encoding="utf-8")
    result = run_shadow(
        catalog=catalog,
        wiki_root=wiki_root,
        vector_probe=lambda *_: True,
        output_dir=tmp_path / "report",
    )

    assert result["status"] == "PHASE8_LOCAL_DEPLOYMENT_POINTER_SHADOW_PASS_NOT_ACTIVATABLE"
    assert result["required_artifact_kinds"] == ["blob", "catalog", "vector_index", "wiki_root"]
    assert result["pointer_is_single_revision"] is True
    assert result["legacy_unchanged"] is True


def test_local_deployment_pointer_report_is_bounded(tmp_path) -> None:
    catalog = tmp_path / "canonical.sqlite3"
    catalog.write_bytes(b"local-catalog")
    wiki_root = tmp_path / "wiki"
    wiki_root.mkdir()
    (wiki_root / "page.md").write_text("local wiki", encoding="utf-8")
    result = run_shadow(catalog=catalog, wiki_root=wiki_root, output_dir=tmp_path / "report")
    report = json.loads((tmp_path / "report/phase8-local-deployment-pointer-shadow-report.json").read_text())

    assert report["status"] == result["status"]
    assert "controlled local deployment failure" not in json.dumps(report)
    assert str(tmp_path) not in json.dumps(report)
