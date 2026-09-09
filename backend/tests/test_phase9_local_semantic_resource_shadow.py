from pathlib import Path


def test_phase9_local_semantic_resource_shadow_is_non_activating(tmp_path: Path) -> None:
    from scripts.phase9_local_semantic_resource_shadow import run_shadow

    report = run_shadow(
        catalog_path=Path("artifacts/phase0b-local-catalog/knowledge-platform.sqlite3"),
        wiki_root=Path("/Users/pet/Documents/knowledge/llm-wiki/wiki"),
        output_path=tmp_path / "semantic-resource-report.json",
    )
    assert report["status"] == "PHASE9_LOCAL_SEMANTIC_RESOURCE_SHADOW_PASS_NOT_ACTIVATABLE"
    assert report["activation_allowed"] is False
    assert report["catalog"]["canonical_unchanged"] is True
    assert report["authorized_read"]["status"] == "ok"
    assert report["wrong_space_read"]["error_code"] == "permission_denied"
    assert report["registry_unbound_read"]["error_code"] == "binding_unavailable"
