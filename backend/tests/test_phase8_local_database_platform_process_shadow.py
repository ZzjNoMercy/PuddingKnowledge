from __future__ import annotations

from scripts.phase8_local_database_platform_process_shadow import _db_summary, _plan_tokens
from scripts.phase8_local_platform_process_server import _local_database_schema_evidence


def test_database_summary_redacts_query_plan_sql_and_keeps_only_bounded_facts() -> None:
    payload = {
        "status": "ok",
        "data": {
            "query_plan": {
                "query_plan_id": "qp_private",
                "sql": "SELECT password FROM private_table",
                "deployment_revision": "local-revision",
            },
            "sql_hash": "sha256:" + "a" * 64,
        },
    }

    summary = _db_summary(payload, 200, phase="generate")

    assert summary["status"] == "ok"
    assert summary["phase"] == "generate"
    assert summary["query_plan"]["sql_hash"].startswith("sha256:")
    assert "private_table" not in str(summary)
    assert "password" not in str(summary)


def test_database_plan_tokens_require_server_owned_id_and_hash() -> None:
    assert _plan_tokens(
        {
            "data": {
                "query_plan": {"query_plan_id": "qp_1"},
                "sql_hash": "sha256:" + "b" * 64,
            }
        }
    ) == ("qp_1", "sha256:" + "b" * 64)
    assert _plan_tokens({"data": {"query_plan": {"query_plan_id": "qp_1"}}}) is None


def test_local_database_shadow_emits_portable_schema_evidence() -> None:
    evidence = _local_database_schema_evidence(source_revision="sha256:" + "a" * 64)

    assert len(evidence) == 1
    assert evidence[0].resource_uri.endswith(
        "/databases/database_insight_data_vehicle_model_base/schema/vehicle_model_base"
    )
    assert evidence[0].revision == "sha256:" + "a" * 64
    assert "/" not in evidence[0].quote
