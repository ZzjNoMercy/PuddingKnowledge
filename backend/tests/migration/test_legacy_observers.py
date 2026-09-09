"""Legacy source-observer tests requiring the original PuddingClaw modules.

These tests are migration evidence, not Platform runtime tests. They require the
legacy source fixture to be supplied separately and are excluded from the default
Platform test traversal.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.migration


def _legacy_root() -> Path:
    value = os.environ.get("PUDDINGKNOWLEDGE_LEGACY_SOURCE", "").strip()
    if not value:
        raise RuntimeError("PUDDINGKNOWLEDGE_LEGACY_SOURCE is required for migration observers")
    return Path(value).expanduser().resolve()

def test_catalog_legacy_observer_is_replayable_and_verifies_real_migration() -> None:
    from scripts.phase0a_catalog_migration_observer import observe

    first = observe()
    second = observe()

    assert first == second
    assert first["result"] == {
        "imported_rows": 2,
        "schema_version": 4,
        "verification_ok": True,
    }
    assert all(item["content_digest_match"] for item in first["evidence"]["tables"])

def test_lease_jobs_legacy_observer_replays_claim_fence_and_drain_semantics() -> None:
    from scripts.phase0a_lease_jobs_observer import observe

    first = observe()
    second = observe()

    assert first == second
    assert first["result"]["heartbeat_owner_a"] is True
    assert first["result"]["wrong_owner_heartbeat"] is False
    assert first["result"]["stale_fence"] == "rejected"
    assert first["result"]["blocked_claim_during_drain"] is True
    assert first["result"]["writes_allowed_in_maintenance"] is False
    assert first["result"]["semantic_claim_after_release"]["status"] == "running"

def test_package_export_legacy_observer_replays_portable_archive_deterministically() -> None:
    from scripts.phase0a_package_export_observer import observe

    first = observe()
    second = observe()

    assert first == second
    assert first["result"]["ready"] is True
    assert first["result"]["required_entries_present"] is True
    assert first["result"]["copied_file_count"] == 1
    assert first["evidence"]["package_manifest_format"] == "analysis-project-package-manifest/v1"
    assert first["evidence"]["package_manifest_checksums_match"] is True
    assert first["evidence"]["portable_binding"] == "spreadsheet"
    assert first["evidence"]["config_binding_count"] == 0
    assert first["evidence"]["raw_config_value_count"] == 0
    assert first["database_side_effects"]["business_rows_written"] == 0
    assert first["database_side_effects"]["catalog_state_unchanged"] is True
    assert first["filesystem_side_effects"]["business_writes"] == []

def test_semantic_authoring_legacy_observer_replays_publish_and_registry_deterministically() -> None:
    from scripts.phase0a_semantic_authoring_observer import observe

    first = observe()
    second = observe()

    assert first == second
    assert first["result"]["plan_valid"] is True
    assert first["result"]["published_ok"] is True
    assert first["result"]["registry_asset_id"] == "measure:golden-measure"
    assert first["evidence"]["published_content_has_formatter"] is True
    assert first["evidence"]["published_content_has_measure_type"] is True
    assert first["database_side_effects"]["active_revision_probe"] == "not_applicable"
    assert first["filesystem_side_effects"]["published_definition_file_count"] == 1

def test_table_query_legacy_observer_replays_catalog_selection_and_safe_execution_deterministically() -> None:
    from scripts.phase0a_table_query_observer import observe

    first = observe()
    second = observe()

    assert first == second
    assert first["result"]["sync"]["answer"] == "按品牌汇总销量：比亚迪 30，蔚来 5。"
    assert first["result"]["sync"]["asset"]["virtual_path"] == "/knowledge/imported/monthly_sales.csv"
    assert first["result"]["async"]["asset"]["asset_id"] is None
    assert first["result"]["same_answer"] is True
    assert first["result"]["sync"]["profile"]["shape"] == [3, 3]
    assert first["evidence"]["sync_generated_code"].startswith("result = df.groupby")
    assert first["evidence"]["async_generated_code"].startswith("result = df.groupby")
    assert first["database_side_effects"]["business_rows_written"] == 0
    assert first["database_side_effects"]["catalog_rows_added"] == 2
    assert first["database_side_effects"]["async_catalog_row_deltas"] == []
    assert first["filesystem_side_effects"]["business_writes"] == []
    assert first["filesystem_side_effects"]["knowledge_root_unchanged"] is True
    assert "/tmp/" not in json.dumps(first, ensure_ascii=False)
    repo_root = _legacy_root()
    for dependency in first["evidence"]["implementation_dependencies"]:
        path = repo_root / dependency["path"]
        assert "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest() == dependency["content_digest"]

def test_database_nl2sql_observer_replays_generation_scope_and_readonly_execution_deterministically() -> None:
    from scripts.phase0a_database_nl2sql_observer import observe

    first = observe()
    second = observe()

    assert first == second
    assert first["result"]["sql"].startswith("SELECT brand, SUM(sales)")
    assert first["result"]["execution"]["rows"] == [
        {"brand": "比亚迪", "total_sales": 42},
        {"brand": "蔚来", "total_sales": 5},
    ]
    assert first["result"]["execution"]["is_complete"] is True
    assert first["evidence"]["validated_table_scope"] == ["sales_facts"]
    assert first["evidence"]["failure_checks"]["unregistered_table"]["status"] == "rejected"
    assert first["evidence"]["failure_checks"]["write_statement"]["status"] == "rejected"
    assert first["evidence"]["production_source_registry"]["supported_types"] == ["mysql", "postgresql"]
    assert first["evidence"]["production_source_registry"]["unchanged"] is True
    assert first["database_side_effects"]["business_rows_written"] == 0
    assert first["database_side_effects"]["unchanged"] is True
    assert first["filesystem_side_effects"]["business_writes"] == []
    assert first["filesystem_side_effects"]["fixture_root_unchanged"] is True
    repo_root = _legacy_root()
    for dependency in first["evidence"]["implementation_dependencies"]:
        path = repo_root / dependency["path"]
        assert "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest() == dependency["content_digest"]

def test_wiki_query_compile_observer_replays_publish_query_and_gap_semantics_deterministically() -> None:
    from scripts.phase0a_wiki_query_compile_observer import observe

    first = observe()
    second = observe()

    assert first == second
    assert first["result"]["compile"] == {
        "published": True,
        "lint_ok": True,
        "lint_counts": {"pages": 1, "errors": 0, "warnings": 0},
        "pages": ["concepts/hybrid-retrieval"],
    }
    assert first["result"]["query"]["pages"][0]["slug"] == "concepts/hybrid-retrieval"
    assert first["result"]["query"]["source_policy"] == "wiki-only"
    assert first["result"]["missing_query"]["knowledge_gap"] is True
    assert first["result"]["missing_query"]["pages"] == []
    assert first["evidence"]["failure_checks"]["bundle_mismatch"]["status"] == "rejected"
    assert first["evidence"]["query_state_unchanged"] is True
    assert first["evidence"]["rejected_publish_state_unchanged"] is True
    assert first["database_side_effects"]["business_rows_written"] == 0
    assert first["filesystem_side_effects"]["business_writes"] == [
        "raw/kb_golden_wiki-3d92479b65/retrieval-note-8822ccbf6b-91d52e42b524.md",
        "wiki/concepts/hybrid-retrieval.md",
    ]
    repo_root = _legacy_root()
    for dependency in first["evidence"]["implementation_dependencies"]:
        path = repo_root / dependency["path"]
        assert "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest() == dependency["content_digest"]

def test_document_rag_observer_replays_chunk_query_and_citations_deterministically() -> None:
    from scripts.phase0a_document_rag_observer import observe

    first = observe()
    second = observe()

    assert first == second
    assert first["result"]["chunks"]["parser"] == "MarkdownNodeParser"
    assert first["result"]["chunks"]["chunk_count"] == 3
    assert first["result"]["query"]["source_count"] == 1
    assert first["result"]["query"]["sources"][0]["uri"] == "/knowledge/imported/operating-policy.md"
    assert first["result"]["query"]["sources"][0]["source_type"] == "knowledge_base"
    assert first["result"]["missing_query"] == {
        "answer_context": "未找到相关内容。",
        "answer_context_digest": "sha256:187bddd5a86b3644414cfa1c66e28afd0a50f8c119c4e58bdc9686fbd873a3cd",
        "source_count": 0,
        "sources": [],
    }
    assert first["evidence"]["provider_boundary"] == "fixed_in_memory_local_retriever"
    assert first["evidence"]["raw_embeddings_recorded"] is False
    assert first["database_side_effects"]["business_rows_written"] == 0
    assert first["filesystem_side_effects"]["business_writes"] == []
    assert first["filesystem_side_effects"]["unchanged"] is True
    repo_root = _legacy_root()
    for dependency in first["evidence"]["implementation_dependencies"]:
        path = repo_root / dependency["path"]
        assert "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest() == dependency["content_digest"]

def test_connectors_capture_observer_replays_oauth_capture_and_incremental_sync_deterministically() -> None:
    from scripts.phase0a_connectors_capture_observer import observe

    first = observe()
    second = observe()

    assert first == second
    assert first["result"]["oauth"]["pkce_method"] == "S256"
    assert first["result"]["oauth"]["wrong_principal_rejected"] is True
    assert first["result"]["oauth"]["session_status"] == "consumed"
    assert first["result"]["oauth"]["temporary_vault_item_removed"] is True
    assert first["result"]["web_capture"]["parse_status"] == "ready"
    assert first["result"]["web_capture"]["same_document_identity"] is True
    assert first["result"]["feishu_sync"]["first_stats"]["changed"] == 1
    assert first["result"]["feishu_sync"]["second_stats"]["unchanged"] == 1
    assert first["result"]["feishu_sync"]["block_calls"] == 1
    assert first["evidence"]["provider_boundary"] == "fixed_http_and_in_memory_feishu_api"
    assert first["evidence"]["remote_network_used"] is False
    assert first["evidence"]["raw_values_recorded"] is False
    assert first["database_side_effects"]["raw_values_in_catalog"] is False
    assert first["filesystem_side_effects"]["knowledge_artifacts_under_root"] is True
    repo_root = _legacy_root()
    for dependency in first["evidence"]["implementation_dependencies"]:
        path = repo_root / dependency["path"]
        assert "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest() == dependency["content_digest"]

def test_table_query_observer_rejects_fixture_path_escape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts import phase0a_table_query_observer as observer

    fixture_path = tmp_path / "invalid.json"
    fixture_path.write_text(
        json.dumps(
            {
                "format": "agent-knowledge-platform-golden-fixture/table-query/v1",
                "sanitized": True,
                "scenario": {
                    "file_name": "../escape.csv",
                    "virtual_path": "/knowledge/imported/../escape.csv",
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(observer, "FIXTURE_PATH", fixture_path)

    with pytest.raises(ValueError, match="simple CSV/TSV filename"):
        observer._fixture()


def test_table_query_declared_no_asset_and_unsafe_code_semantics_are_executable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tools import pandas_knowledge_tool as pandas_tool_module
    from utils.table_engine.errors import PandasQueryEngineError
    from utils.table_engine.runner import InProcessPandasRunner

    monkeypatch.setenv("PUDDINGCLAW_KNOWLEDGE_DIR", str(tmp_path / "knowledge"))
    monkeypatch.setattr(
        pandas_tool_module.PandasKnowledgeQueryTool,
        "_list_table_assets_from_catalog",
        lambda self, **_kwargs: [],
    )
    no_asset = pandas_tool_module.PandasKnowledgeQueryTool(base_dir=str(tmp_path)).query_structured("汇总销量")
    assert no_asset["asset"] is None
    assert no_asset["engine_metadata"]["reason"] == "no table assets"

    with pytest.raises(PandasQueryEngineError):
        InProcessPandasRunner().run(object(), "result = open('/etc/passwd').read()")
