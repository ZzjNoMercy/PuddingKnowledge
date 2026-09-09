from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from knowledge_platform.baseline import (
    capture_baseline,
    capture_baseline_record_document,
    compare_baseline,
    normalize_for_baseline,
    normalized_digest,
)


def test_baseline_normalization_is_order_stable_and_redacts_secrets_paths_and_urls() -> None:
    left = {
        "z": ["/private/a.md", "https://example.test/a?token=secret"],
        "password": "plain-secret",
        "nested": {"api-key": "key-value", "answer": "stable"},
    }
    right = {
        "nested": {"answer": "stable", "api-key": "other-key"},
        "password": "other-secret",
        "z": ["/private/a.md", "https://example.test/a?token=changed"],
    }

    assert normalized_digest(left) == normalized_digest(right)
    assert normalize_for_baseline(left)["password"] == "<redacted>"
    assert normalize_for_baseline(left)["z"] == ["<path-redacted>", "<url-redacted>"]


def test_baseline_separates_result_evidence_and_side_effect_digests() -> None:
    record = capture_baseline(
        capability_id="document-rag",
        source_revision="source-rev-1",
        result={"answer": "answer"},
        evidence=[{"asset_id": "asset-1", "uri": "knowledge://asset/1"}],
        database_side_effects={"writes": []},
        filesystem_side_effects={"writes": []},
        provider_revision="index-rev-1",
        failure_semantics="invalid_request->stable_error",
        sanitized_fixture_manifest={"fixtures": ["fixture-1"]},
    )

    assert record.normalized_result_digest != record.evidence_digest
    assert set(record.to_dict()) == {
        "capability_id",
        "source_revision",
        "normalized_result_digest",
        "evidence_digest",
        "database_side_effect_digest",
        "filesystem_side_effect_digest",
        "provider_revision",
        "failure_semantics",
        "sanitized_fixture_manifest_digest",
    }


def test_baseline_comparison_reports_changed_side_effect_without_masking_it() -> None:
    common = dict(
        capability_id="wiki-query",
        source_revision="source-rev-1",
        result={"answer": "same"},
        evidence=[],
        filesystem_side_effects={"writes": []},
        provider_revision="provider-1",
        failure_semantics="success",
        sanitized_fixture_manifest={"fixtures": ["fixture-1"]},
    )
    expected = capture_baseline(database_side_effects={"writes": []}, **common)
    observed = capture_baseline(database_side_effects={"writes": ["unexpected"]}, **common)

    assert compare_baseline(expected, observed) == ("database_side_effect_digest",)


def test_record_document_retain_normalized_observations_and_matching_digests() -> None:
    document = capture_baseline_record_document(
        capability_id="document-rag",
        source_revision="sha256:" + "a" * 64,
        result={"answer": "stable", "source_path": "/host/private.md"},
        evidence={"items": [{"uri": "https://example.test/evidence"}]},
        database_side_effects={"writes": []},
        filesystem_side_effects={"writes": []},
        provider_revision="provider-1",
        failure_semantics="invalid_request->stable_error",
        sanitized_fixture_manifest={"fixtures": ["fixtures/document.json"]},
    )

    assert document["result"] == {"answer": "stable", "source_path": "<path-redacted>"}
    assert document["evidence"] == {"items": [{"uri": "<url-redacted>"}]}
    digest_record = capture_baseline(
        capability_id=document["capability_id"],
        source_revision=document["source_revision"],
        result=document["result"],
        evidence=document["evidence"],
        database_side_effects=document["database_side_effects"],
        filesystem_side_effects=document["filesystem_side_effects"],
        provider_revision=document["provider_revision"],
        failure_semantics=document["failure_semantics"],
        sanitized_fixture_manifest=document["sanitized_fixture_manifest"],
    )
    assert digest_record.normalized_result_digest == normalized_digest(document["result"])
    assert digest_record.evidence_digest == normalized_digest(document["evidence"])
    assert digest_record.sanitized_fixture_manifest_digest == normalized_digest(document["sanitized_fixture_manifest"])


def test_record_document_rejects_secret_bearing_observation_keys() -> None:
    with pytest.raises(ValueError, match="secret-bearing key"):
        capture_baseline_record_document(
            capability_id="document-rag",
            source_revision="sha256:" + "a" * 64,
            result={"api_key": "must-not-be-recorded"},
            evidence={},
            database_side_effects={},
            filesystem_side_effects={},
            provider_revision="provider-1",
            failure_semantics="success",
            sanitized_fixture_manifest={"fixtures": ["fixtures/document.json"]},
        )


def test_record_document_sorts_and_validates_fixture_paths() -> None:
    document = capture_baseline_record_document(
        capability_id="document-rag",
        source_revision="sha256:" + "a" * 64,
        result={},
        evidence={},
        database_side_effects={},
        filesystem_side_effects={},
        provider_revision="provider-1",
        failure_semantics="success",
        sanitized_fixture_manifest={"fixtures": ["fixtures/z.json", "fixtures/a.json"]},
    )

    assert document["sanitized_fixture_manifest"] == {
        "fixtures": ["fixtures/a.json", "fixtures/z.json"]
    }
    with pytest.raises(ValueError, match="repository-relative"):
        capture_baseline_record_document(
            capability_id="document-rag",
            source_revision="sha256:" + "a" * 64,
            result={},
            evidence={},
            database_side_effects={},
            filesystem_side_effects={},
            provider_revision="provider-1",
            failure_semantics="success",
            sanitized_fixture_manifest={"fixtures": ["../outside.json"]},
        )


def test_baseline_record_cli_captures_explicit_observer(tmp_path: Path) -> None:
    module = tmp_path / "capture_fixture.py"
    module.write_text(
        "def observe():\n"
        "    return {\n"
        "        'result': {'answer': 'ok'},\n"
        "        'evidence': {'items': []},\n"
        "        'database_side_effects': {'writes': []},\n"
        "        'filesystem_side_effects': {'writes': []},\n"
        "        'provider_revision': 'provider-1',\n"
        "        'failure_semantics': 'success',\n"
        "        'sanitized_fixture_manifest': {'fixtures': ['fixtures/document.json']},\n"
        "    }\n",
        encoding="utf-8",
    )
    script = Path(__file__).resolve().parents[1] / "scripts" / "phase0a_baseline_record.py"
    environment = {"PYTHONPATH": f"{tmp_path}:{Path(__file__).resolve().parents[1]}"}

    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--observer",
            "capture_fixture:observe",
            "--capability-id",
            "document_rag",
            "--source-revision",
            "sha256:" + "a" * 64,
        ],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    document = json.loads(result.stdout)

    assert document["format"] == "agent-knowledge-platform-golden-baseline-record/v1"
    assert document["capability_id"] == "document_rag"
    assert set(document) == {
        "format",
        "capability_id",
        "source_revision",
        "result",
        "evidence",
        "database_side_effects",
        "filesystem_side_effects",
        "provider_revision",
        "failure_semantics",
        "sanitized_fixture_manifest",
    }


def test_golden_replay_report_distinguishes_match_from_unfrozen_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts import phase0a_golden_replay as replay

    source_revision = "sha256:" + "a" * 64
    observation = {
        "result": {"answer": "ok"},
        "evidence": {"items": []},
        "database_side_effects": {"writes": []},
        "filesystem_side_effects": {"writes": []},
        "provider_revision": "provider-1",
        "failure_semantics": "success",
        "sanitized_fixture_manifest": {"fixtures": ["fixture.json"]},
    }
    expected = replay.capture_baseline_record_document(
        capability_id="demo",
        source_revision=source_revision,
        **observation,
    )
    record_path = tmp_path / "record.json"
    record_path.write_text(json.dumps(expected), encoding="utf-8")
    monkeypatch.setattr(
        replay,
        "load_fixture_manifest",
        lambda *_args, **_kwargs: (
            {
                "capabilities": [
                    {
                        "id": "demo",
                        "baseline": {"source_revision": source_revision},
                        "baseline_record_file": {"path": "record.json"},
                    }
                ]
            },
            SimpleNamespace(status="capture-required", frozen=False, capability_ids=("demo",)),
        ),
    )
    monkeypatch.setattr(
        replay,
        "load_legacy_runtime_probe_registry",
        lambda *_args, **_kwargs: (
            {"executed_probes": [{"capability_id": "demo", "reference": "demo:observe"}]},
            SimpleNamespace(status="pending", reviewed=False, pending_boundary_review_ids=("demo",)),
        ),
    )
    monkeypatch.setattr(replay, "_resolve_observer", lambda _reference: lambda: observation)

    report = replay.replay_golden_baselines(
        repo_root=tmp_path,
        fixture_manifest_path=tmp_path / "fixture.yaml",
        runtime_registry_path=tmp_path / "runtime.yaml",
    )

    assert report["status"] == "GOLDEN_REPLAY_MATCHED_NOT_FROZEN"
    assert report["summary"] == {
        "freeze_claim_allowed": False,
        "matched_count": 1,
        "mismatched_count": 0,
        "not_replayable_count": 0,
    }
