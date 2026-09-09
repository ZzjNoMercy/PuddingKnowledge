from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

from knowledge_contracts import (
    CAPABILITIES,
    CapabilityDescriptor,
    CitationCandidate,
    Evidence,
    Job,
    JobStatus,
    NotificationEvent,
    Provenance,
    QueryError,
    QueryErrorCode,
    QueryPlan,
    QueryPlanValidation,
    QueryResult,
)


def test_contracts_are_framework_neutral() -> None:
    module = importlib.import_module("knowledge_contracts.query")
    source = module.__loader__.get_source(module.__name__)  # type: ignore[attr-defined]
    assert source is not None
    forbidden = ("fastapi", "pydantic", "sqlalchemy", "langchain", "langgraph", "mcp")
    assert not any(f"import {name}" in source or f"from {name}" in source for name in forbidden)


def test_contract_module_does_not_load_runtime_frameworks() -> None:
    loaded_before = set(sys.modules)
    importlib.import_module("knowledge_contracts")
    newly_loaded = set(sys.modules) - loaded_before
    assert not any(name.split(".")[0] in {"fastapi", "sqlalchemy", "langchain", "langgraph"} for name in newly_loaded)


def test_query_result_serializes_evidence_and_provenance() -> None:
    evidence = Evidence(
        asset_id="asset_1",
        resource_uri="knowledge://spaces/acme/assets/asset_1",
        locator={"page": 12},
        quote="收入确认",
        score=0.91,
        revision="sha256:" + "a" * 64,
        matched_by=("vector", "bm25"),
    )
    result = QueryResult(
        status="ok",
        answer="42",
        data={"columns": ["value"], "rows": [[42]]},
        evidence=(evidence,),
        provenance=Provenance(
            space_id="space_acme",
            dataset_id="dataset_sales",
            dataset_version="1.2.0",
            capability="document_rag_query",
        ),
        trace_id="trace_1",
    )
    assert result.to_dict()["evidence"][0]["locator"] == {"page": 12}
    assert result.to_dict()["provenance"]["capability"] == "document_rag_query"
    json.dumps(result.to_dict())


def test_error_result_requires_stable_error_contract() -> None:
    with pytest.raises(ValueError, match="must carry an error"):
        QueryResult(status="error")
    result = QueryResult(
        status="error",
        error=QueryError(QueryErrorCode.INDEX_NOT_READY, "index is warming", retryable=True),
    )
    assert result.to_dict()["error"]["code"] == QueryErrorCode.INDEX_NOT_READY
    json.dumps(result.to_dict())


def test_query_plan_is_only_issuable_after_all_readonly_guards_pass() -> None:
    with pytest.raises(ValueError, match="validation must pass"):
        QueryPlan(
            query_plan_id="qp_1",
            sql="select 1",
            dialect="postgresql",
            dataset_id="dataset_1",
            dataset_version="1.0.0",
            deployment_revision="deploy_1",
            semantic_context_hash="sha256:ctx",
            validation=QueryPlanValidation(True, True, False),
            expires_at="2026-09-02T00:00:00Z",
        )
    assert "query_plan_id" in QueryPlan(
        query_plan_id="qp_1",
        sql="select 1",
        dialect="postgresql",
        dataset_id="dataset_1",
        dataset_version="1.0.0",
        deployment_revision="deploy_1",
        semantic_context_hash="sha256:ctx",
        validation=QueryPlanValidation(True, True, True),
        expires_at="2026-09-02T00:00:00Z",
    ).to_dict()
    assert "database_execute_readonly" in CAPABILITIES
    assert "database_schema" in CAPABILITIES
    assert "capture_processing" in CAPABILITIES
    assert "connector_sync" in CAPABILITIES
    assert "gbrain_projection" in CAPABILITIES


def test_security_boolean_guards_do_not_accept_truthy_strings() -> None:
    with pytest.raises(TypeError, match="must be bools"):
        QueryPlanValidation("true", True, True)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="must be bools"):
        CapabilityDescriptor("cap", "provider", "read", True, "false", ())  # type: ignore[arg-type]


def test_evidence_and_correlation_reject_non_portable_boundary_data() -> None:
    from knowledge_contracts import Correlation

    with pytest.raises(ValueError, match="opaque"):
        Correlation("/var/run/request.log")
    with pytest.raises(ValueError, match="trace_id"):
        Correlation(None)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="resource_uri"):
        Evidence("asset_1", "knowledge://../etc/passwd")
    with pytest.raises(ValueError, match="locator"):
        Evidence("asset_1", "knowledge://asset/1", locator={"section": "file:///etc/passwd"})
    with pytest.raises(ValueError, match="locator"):
        Evidence("asset_1", "knowledge://asset/1", locator={"section": "unsafe\u0000text"})
    with pytest.raises(ValueError, match="quote"):
        Evidence("asset_1", "knowledge://asset/1", quote="unsafe\u0000text")
    with pytest.raises(ValueError, match="score"):
        Evidence("asset_1", "knowledge://asset/1", score=float("nan"))
    with pytest.raises(ValueError, match="asset_id"):
        Evidence("/etc/passwd", "knowledge://asset/1")
    with pytest.raises(ValueError, match="quote"):
        Evidence("asset_1", "knowledge://asset/1", quote="path=/etc/passwd")
    with pytest.raises(ValueError, match="matched_by"):
        Evidence("asset_1", "knowledge://asset/1", matched_by=("file:///etc/passwd",))
    with pytest.raises(ValueError, match="CitationCandidate.asset_id"):
        CitationCandidate("/etc/passwd", "knowledge://asset/1")


def test_job_contract_has_platform_identity_without_harness_session_fields() -> None:
    job = Job("job_1", "document_import", JobStatus.QUEUED, "space_1")
    assert job.to_dict() == {
        "job_id": "job_1",
        "kind": "document_import",
        "status": JobStatus.QUEUED,
        "space_id": "space_1",
        "progress": 0,
        "revision": None,
        "error": None,
    }


def test_notification_event_contract_has_no_inbox_read_state() -> None:
    event = NotificationEvent(
        event_id="notification_1",
        event_type="task_notification.v1",
        subject_type="semantic_dimension_build_job",
        subject_id="job_1",
        title="Build published",
        occurred_at="2026-09-03T01:05:00Z",
        payload={"status": "published"},
    )
    assert "read_at" not in event.to_dict()
    assert event.to_dict()["payload"] == {"status": "published"}


def test_notification_display_text_rejects_secret_and_path_material() -> None:
    with pytest.raises(ValueError, match="display text"):
        NotificationEvent(
            event_id="notification_1",
            event_type="task_notification.v1",
            subject_type="job",
            subject_id="job_1",
            title="token=super-secret",
            occurred_at="2026-09-03T01:05:00Z",
        )
    with pytest.raises(ValueError, match="display text"):
        NotificationEvent(
            event_id="notification_1",
            event_type="task_notification.v1",
            subject_type="job",
            subject_id="job_1",
            title="path=/etc/passwd",
            occurred_at="2026-09-03T01:05:00Z",
        )


def test_boundary_mappings_are_recursively_immutable() -> None:
    result = QueryResult(status="ok", data={"rows": [{"value": 1}]})
    with pytest.raises(TypeError):
        result.data["rows"][0]["value"] = 2

    event = NotificationEvent(
        event_id="notification_1",
        event_type="task_notification.v1",
        subject_type="job",
        subject_id="job_1",
        title="Job complete",
        occurred_at="2026-09-03T01:05:00Z",
        payload={"status": "published"},
    )
    with pytest.raises(TypeError):
        event.payload["status"] = "failed"
    with pytest.raises(TypeError):
        dict.__setitem__(result.data, "bypass", True)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="non-JSON"):
        QueryResult(status="ok", data={"blob": bytearray(b"a")})


def test_notification_event_contract_has_machine_readable_schema() -> None:
    from jsonschema import Draft202012Validator

    event = NotificationEvent(
        event_id="notification_1",
        event_type="task_notification.v1",
        subject_type="job",
        subject_id="job_1",
        title="Build published",
        occurred_at="2026-09-03T01:05:00Z",
        payload={"status": "published"},
    )
    schema = json.loads(
        (Path(__file__).resolve().parents[1] / "knowledge_contracts/schemas/notification-event.schema.json").read_text()
    )
    assert list(Draft202012Validator(schema).iter_errors(event.to_dict())) == []


def test_public_schemas_reject_path_traversal_and_sensitive_display_text() -> None:
    from jsonschema import Draft202012Validator

    root = Path(__file__).resolve().parents[1] / "knowledge_contracts/schemas"
    query_schema = json.loads((root / "query-result.schema.json").read_text())
    query_errors = list(
        Draft202012Validator(query_schema).iter_errors(
            {
                "status": "ok",
                "answer": "",
                "data": {},
                "evidence": [
                    {
                        "asset_id": "asset_1",
                        "resource_uri": "knowledge://../etc/passwd",
                        "locator": {},
                        "quote": "",
                        "revision": "",
                        "matched_by": [],
                    }
                ],
                "warnings": [],
                "trace_id": "trace_1",
            }
        )
    )
    assert query_errors
    query_locator_errors = list(
        Draft202012Validator(query_schema).iter_errors(
            {
                "status": "ok",
                "answer": "",
                "data": {},
                "evidence": [
                    {
                        "asset_id": "asset_1",
                        "resource_uri": "knowledge://spaces/space_1/assets/asset_1",
                        "locator": {"section": "/Users/pet/private.txt"},
                        "quote": "",
                        "revision": "",
                        "matched_by": [],
                    }
                ],
                "warnings": [],
                "trace_id": "trace_1",
            }
        )
    )
    assert query_locator_errors

    notification_schema = json.loads((root / "notification-event.schema.json").read_text())
    notification_errors = list(
        Draft202012Validator(notification_schema).iter_errors(
            {
                "event_id": "notification_1",
                "event_type": "task_notification.v1",
                "subject_type": "job",
                "subject_id": "job_1",
                "title": "path=/etc/passwd",
                "body": "",
                "payload": {},
                "occurred_at": "2026-09-03T01:05:00Z",
            }
        )
    )
    assert notification_errors


def test_mcp_resource_schema_is_versioned_and_fail_closed() -> None:
    from jsonschema import Draft202012Validator

    schema_path = Path(__file__).resolve().parents[1] / "knowledge_contracts/schemas/mcp-resource.schema.json"
    schema = json.loads(schema_path.read_text())
    assert schema["$id"] == "https://puddingknowledge.dev/contracts/mcp-resource/v1"
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)

    assert list(validator.iter_errors({
        "resources": [{"uri": "knowledge://spaces/space_1/manifest", "name": "Space"}],
        "resourceTemplates": [{"uriTemplate": "knowledge://spaces/{space_id}/assets/{asset_id}"}],
    })) == []
    assert list(validator.iter_errors({
        "contents": [{
            "uri": "knowledge://spaces/space_1/manifest",
            "mimeType": "text/markdown",
            "text": "# safe",
        }],
        "structuredContent": {"provenance": {"capability": "wiki_query"}, "row_count": 0, "ready": True, "optional": None},
    })) == []
    for invalid in (
        {"resources": [{"uri": "file:///Users/pet/private.md"}], "resourceTemplates": []},
        {"contents": [{"uri": "knowledge://spaces/space_1/manifest", "text": "path=/Users/pet/private.md"}]},
        {"contents": [{"uri": "knowledge://spaces/space_1/manifest", "text": "ok", "blob": "Yg=="}]},
        {"contents": [{"uri": "knowledge://spaces/space_1/manifest", "text": "ok"}], "structuredContent": {"artifact_path": "/private/secret"}},
    ):
        assert list(validator.iter_errors(invalid)), invalid
