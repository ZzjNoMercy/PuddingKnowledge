from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from knowledge_platform.baseline import (
    load_runtime_probe_registry,
    validate_runtime_probe_registry,
)
from scripts import phase0a_runtime_probe_registry as probe_registry


def _document(*, status: str = "observation-primitive-ready-coverage-not-complete") -> dict[str, object]:
    return {
        "format": "agent-native-knowledge-platform-runtime-call-probes/v1",
        "spec_revision": "v0.7",
        "status": status,
        "coverage_scope": "platform-contract-observation-only",
        "legacy_runtime_coverage": "not-claimed",
        "required_probe_families": ["retrieval_and_evidence"],
        "executed_probes": [],
    }


def _write_valid_observation_output(path: Path, *, probe_reference: str = "probe_module:run") -> None:
    observed = {
        "edges": [
            {
                "caller_module": "knowledge_platform.probe",
                "caller_function": "run",
                "callee_module": "knowledge_platform.target",
                "callee_function": "call",
            }
        ],
        "loaded_modules": ["knowledge_platform.probe", "knowledge_platform.target"],
    }
    observed["graph_digest"] = "sha256:" + hashlib.sha256(
        json.dumps(observed, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    path.write_text(
        json.dumps(
            {
                "format": "agent-native-knowledge-platform-runtime-call-graph-report/v1",
                "observation_only": True,
                "probes": [
                    {
                        **observed,
                        "format": "agent-knowledge-platform-runtime-call-graph/v1",
                        "probe_reference": probe_reference,
                        "module_prefixes": ["knowledge_platform"],
                        "observation_only": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def test_checked_in_runtime_registry_is_explicitly_incomplete() -> None:
    root = Path(__file__).resolve().parents[2]
    document, validation = load_runtime_probe_registry(
        root / "docs/knowledge-platform/phase-0a-runtime-call-probes.yaml",
        repo_root=root,
    )

    assert document["status"] == "observation-primitive-ready-coverage-not-complete"
    assert validation.complete is False
    assert validation.required_families == (
        "knowledge_catalog_and_processing",
        "retrieval_and_evidence",
        "wiki_compilation",
        "structured_query_and_vanna_boundary",
        "semantic_authoring",
        "harness_wiring_boundary",
    )


def test_checked_in_runtime_registry_replays_all_captured_outputs() -> None:
    result = subprocess.run(
        [
            "backend/.venv/bin/python",
            "backend/scripts/phase0a_runtime_probe_registry.py",
            "--replay",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    report = json.loads(result.stdout)

    assert report["replay"]["status"] == "matched"
    assert report["replay"]["mismatched_probe_ids"] == []
    assert report["replay"]["replayed_probe_ids"] == report["executed_probe_ids"]


def test_require_complete_always_replays_before_applying_strict_gate() -> None:
    result = subprocess.run(
        [
            "backend/.venv/bin/python",
            "backend/scripts/phase0a_runtime_probe_registry.py",
            "--require-complete",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    report = json.loads(result.stdout)

    assert result.returncode == 1
    assert report["error"] == "runtime probe registry is not complete"
    assert report["replay"]["status"] == "matched"
    assert report["replay"]["mismatched_probe_ids"] == []


def test_complete_registry_requires_every_family_output_and_review() -> None:
    document = _document(status="complete")
    with pytest.raises(ValueError, match="does not cover every required family"):
        validate_runtime_probe_registry(document, repo_root=Path("."))


def test_complete_registry_requires_output_even_when_reviewed() -> None:
    document = _document(status="complete")
    document["executed_probes"] = [
        {
            "id": "retrieval_probe",
            "family": "retrieval_and_evidence",
            "reference": "probe_module:run",
            "module_prefixes": ["knowledge_platform"],
            "scope": "platform-contract-observation-only",
            "boundary_review": {"status": "reviewed"},
        }
    ]

    with pytest.raises(ValueError, match="output must be a mapping"):
        validate_runtime_probe_registry(document, repo_root=Path("."), require_complete=True)


def test_complete_registry_validates_content_addressed_output(tmp_path: Path) -> None:
    output = tmp_path / "runtime-report.json"
    observed = {
        "edges": [
            {
                "caller_module": "knowledge_platform.probe",
                "caller_function": "run",
                "callee_module": "knowledge_platform.target",
                "callee_function": "call",
            }
        ],
        "loaded_modules": ["knowledge_platform.probe", "knowledge_platform.target"],
    }
    observed["graph_digest"] = "sha256:" + hashlib.sha256(
        json.dumps(observed, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    report = {
        "format": "agent-native-knowledge-platform-runtime-call-graph-report/v1",
        "observation_only": True,
        "probes": [
            {
                **observed,
                "format": "agent-knowledge-platform-runtime-call-graph/v1",
                "probe_reference": "probe_module:run",
                "module_prefixes": ["knowledge_platform"],
                "observation_only": True,
            }
        ],
    }
    output.write_text(json.dumps(report), encoding="utf-8")
    document = _document(status="complete")
    document["executed_probes"] = [
        {
            "id": "retrieval_probe",
            "family": "retrieval_and_evidence",
            "reference": "probe_module:run",
            "module_prefixes": ["knowledge_platform"],
            "scope": "platform-contract-observation-only",
            "output": {
                "path": output.name,
                "bytes": output.stat().st_size,
                "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
            },
            "boundary_review": {"status": "reviewed"},
        }
    ]

    validation = validate_runtime_probe_registry(
        document,
        repo_root=tmp_path,
        require_complete=True,
        replay_evidence={
            "status": "matched",
            "replayed_probe_ids": ["retrieval_probe"],
            "mismatched_probe_ids": [],
            "skipped_probe_ids": [],
        },
    )

    assert validation.complete is True
    assert validation.executed_probe_ids == ("retrieval_probe",)


def test_complete_registry_requires_replay_evidence(tmp_path: Path) -> None:
    output = tmp_path / "runtime-report.json"
    _write_valid_observation_output(output)
    document = _document(status="complete")
    document["executed_probes"] = [
        {
            "id": "retrieval_probe",
            "family": "retrieval_and_evidence",
            "reference": "probe_module:run",
            "module_prefixes": ["knowledge_platform"],
            "scope": "platform-contract-observation-only",
            "output": {
                "path": output.name,
                "bytes": output.stat().st_size,
                "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
            },
            "boundary_review": {"status": "reviewed"},
        }
    ]

    with pytest.raises(ValueError, match="requires replay evidence"):
        validate_runtime_probe_registry(document, repo_root=tmp_path, require_complete=True)


def test_observation_output_rejects_malformed_inner_graph(tmp_path: Path) -> None:
    output = tmp_path / "runtime-report.json"
    report = {
        "format": "agent-native-knowledge-platform-runtime-call-graph-report/v1",
        "observation_only": True,
        "probes": [
            {
                "format": "agent-knowledge-platform-runtime-call-graph/v1",
                "observation_only": True,
                "probe_reference": "probe_module:run",
                "module_prefixes": ["knowledge_platform"],
                "loaded_modules": [],
                "edges": [],
                "graph_digest": "sha256:" + "0" * 64,
            }
        ],
    }
    output.write_text(json.dumps(report), encoding="utf-8")
    document = _document(status="complete")
    document["executed_probes"] = [
        {
            "id": "retrieval_probe",
            "family": "retrieval_and_evidence",
            "reference": "probe_module:run",
            "module_prefixes": ["knowledge_platform"],
            "scope": "platform-contract-observation-only",
            "output": {
                "path": output.name,
                "bytes": output.stat().st_size,
                "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
            },
            "boundary_review": {"status": "reviewed"},
        }
    ]

    with pytest.raises(ValueError, match="edges must be a non-empty list"):
        validate_runtime_probe_registry(document, repo_root=tmp_path, require_complete=False)


def test_registry_rejects_duplicate_yaml_keys(tmp_path: Path) -> None:
    path = tmp_path / "probes.yaml"
    path.write_text(
        "format: agent-native-knowledge-platform-runtime-call-probes/v1\n"
        "format: duplicate\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate YAML key"):
        load_runtime_probe_registry(path)


def test_complete_registry_rejects_output_digest_drift(tmp_path: Path) -> None:
    output = tmp_path / "runtime-report.json"
    output.write_text("actual\n", encoding="utf-8")
    document = _document(status="complete")
    document["executed_probes"] = [
        {
            "id": "retrieval_probe",
            "family": "retrieval_and_evidence",
            "reference": "probe_module:run",
            "module_prefixes": ["knowledge_platform"],
            "scope": "platform-contract-observation-only",
            "output": {"path": output.name, "bytes": output.stat().st_size, "sha256": "0" * 64},
            "boundary_review": {"status": "reviewed"},
        }
    ]

    with pytest.raises(ValueError, match="digest drift"):
        validate_runtime_probe_registry(document, repo_root=tmp_path, require_complete=True)


def test_partial_registry_validates_observation_output_identity(tmp_path: Path) -> None:
    output = tmp_path / "runtime-report.json"
    _write_valid_observation_output(output)
    document = _document()
    document["executed_probes"] = [
        {
            "id": "retrieval_probe",
            "family": "retrieval_and_evidence",
            "reference": "probe_module:run",
            "module_prefixes": ["knowledge_platform"],
            "scope": "platform-contract-observation-only",
            "output": {
                "path": output.name,
                "bytes": output.stat().st_size,
                "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
            },
            "boundary_review": {"status": "pending"},
        }
    ]

    validation = validate_runtime_probe_registry(document, repo_root=tmp_path)

    assert validation.complete is False
    assert validation.executed_probe_ids == ("retrieval_probe",)


def test_partial_registry_rejects_probe_output_identity_drift(tmp_path: Path) -> None:
    output = tmp_path / "runtime-report.json"
    _write_valid_observation_output(output, probe_reference="other:run")
    document = _document()
    document["executed_probes"] = [
        {
            "id": "retrieval_probe",
            "family": "retrieval_and_evidence",
            "reference": "probe_module:run",
            "module_prefixes": ["knowledge_platform"],
            "scope": "platform-contract-observation-only",
            "output": {
                "path": output.name,
                "bytes": output.stat().st_size,
                "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
            },
        }
    ]

    with pytest.raises(ValueError, match="probe reference"):
        validate_runtime_probe_registry(document, repo_root=tmp_path)


def test_registry_rejects_scope_that_claims_legacy_coverage() -> None:
    document = _document()
    document["coverage_scope"] = "legacy-runtime"

    with pytest.raises(ValueError, match="platform-contract observation scope"):
        validate_runtime_probe_registry(document)


def test_replay_marks_timeout_as_mismatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output = tmp_path / "runtime-report.json"
    output.write_text("{}", encoding="utf-8")
    document = {
        "executed_probes": [
            {
                "id": "retrieval_probe",
                "reference": "probe_module:run",
                "module_prefixes": ["knowledge_platform"],
                "output": {"path": output.name},
            }
        ]
    }

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(kwargs.get("args", args[0]), 30)

    monkeypatch.setattr(probe_registry.subprocess, "run", timeout)

    report = probe_registry._replay_probe_outputs(document, repo_root=tmp_path)

    assert report["status"] == "mismatch"
    assert report["mismatched_probe_ids"] == ["retrieval_probe"]
