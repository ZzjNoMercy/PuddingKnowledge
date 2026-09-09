"""Small target-owned inputs for tests that used to read mixed-tree artifacts."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from knowledge_platform.baseline import capture_baseline_record_document
from scripts.phase0a_golden_dependency_review_queue import build_review_queue


def write_evidence_reports(root: Path) -> None:
    reports = root / "artifacts/phase0b-local-catalog"
    reports.mkdir(parents=True, exist_ok=True)
    reports_by_name: dict[str, dict[str, Any]] = {
        "phase8-local-capability-matrix-shadow-report.json": {
            "capability_count": 1,
            "pass_count": 1,
            "independent_process_count": 1,
            "clean_shutdown_count": 1,
            "network_contacted": False,
            "external_network_contacted": False,
            "status": "LOCAL_CAPABILITY_MATRIX_PASS_NOT_ACTIVATABLE",
        },
        "phase9-local-distribution-matrix-shadow-report.json": {
            "check_count": 1,
            "pass_count": 1,
            "activation_allowed": False,
            "release_execution_allowed": False,
            "network_contacted": False,
            "independent_repositories_created": False,
            "status": "PHASE9_LOCAL_DISTRIBUTION_MATRIX_PASS_NOT_ACTIVATABLE",
        },
        "phase10-package-build-shadow.json": {
            "command_count": 1,
            "all_commands_passed": True,
            "replay_consistent": True,
            "activation_allowed": False,
            "release_execution_allowed": False,
            "network_contacted": False,
            "status": "PHASE10_PACKAGE_BUILD_PASS_NOT_ACTIVATABLE",
        },
        "phase10-dependency-sbom-shadow.json": {
            "python_component_count": 1,
            "node_component_count": 0,
            "replay_consistent": True,
            "network_contacted": False,
            "dependency_install_performed": False,
            "release_artifact_generated": False,
            "status": "PHASE10_DEPENDENCY_SBOM_PASS_NOT_ACTIVATABLE",
        },
        "phase10-extraction-preflight.json": {
            "replay_consistent": True,
            "executable": False,
            "activation_allowed": False,
            "source_tag_signed": False,
            "source_worktree_clean": False,
            "missing_mixed_file_paths": [],
            "status": "PHASE10_EXTRACTION_PREFLIGHT_NOT_EXECUTABLE",
        },
        "phase10-rc-validation-shadow.json": {
            "releaseable": False,
            "release_rollback_window_open": True,
            "status": "PHASE10_RC_VALIDATION_BLOCKED",
        },
        "phase10-local-installation-migration-real-shadow.json": {
            "replay_idempotent": True,
            "failure_recovery_verified": True,
            "partial_target_import_replay_verified": True,
            "post_cutover_delta_verified": True,
            "credential_rebind_recovery_verified": True,
            "rollback_reconciliation_verified": True,
            "stateful_rollback_replay_verified": True,
            "canonical_catalog_changed": False,
            "physical_copy_performed": False,
            "secret_bytes_read": False,
            "snapshot": {
                "source_manifest_verified": True,
                "source_manifest_digest": "sha256:" + "1" * 64,
                "snapshot_digest": "sha256:" + "2" * 64,
            },
            "status": "PHASE10_LOCAL_INSTALLATION_MIGRATION_PASS_NOT_ACTIVATABLE",
        },
        "phase-gates-latest.json": {
            "phases": [{"phase_id": "phase_0a", "ready": False}],
            "blockers": ["synthetic target fixture"],
            "ready": False,
            "status": "PHASE_GATES_BLOCKED",
        },
    }
    for name, payload in reports_by_name.items():
        (reports / name).write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")


def golden_dependency_inputs(root: Path) -> tuple[Path, Path, str, dict[str, object]]:
    """Create a self-contained candidate/canonical/replay queue fixture."""

    root = root / "inputs"
    root.mkdir(parents=True, exist_ok=True)
    observation: dict[str, object] = {
        "result": {"status": "ok", "captured": True},
        "evidence": {
            "implementation_dependencies": [
                {"path": "backend/scripts/phase0a_connectors_capture_observer.py", "sha256": "a" * 64},
                {"path": "backend/knowledge_platform/contracts.py", "sha256": "b" * 64},
            ]
        },
        "database_side_effects": {"writes": 0},
        "filesystem_side_effects": {"writes": 0},
        "provider_revision": "synthetic-provider-v1",
        "failure_semantics": "invalid_input->rejected; valid_input->captured",
        "sanitized_fixture_manifest": {"fixtures": ["docs/knowledge-platform/golden-fixtures/connectors_and_capture.json"]},
    }
    candidate = capture_baseline_record_document(
        capability_id="connectors_and_capture",
        source_revision="sha256:" + "c" * 64,
        result=observation["result"],
        evidence=observation["evidence"],
        database_side_effects=observation["database_side_effects"],
        filesystem_side_effects=observation["filesystem_side_effects"],
        provider_revision=str(observation["provider_revision"]),
        failure_semantics=str(observation["failure_semantics"]),
        sanitized_fixture_manifest=observation["sanitized_fixture_manifest"],
    )
    canonical = copy.deepcopy(candidate)
    canonical["evidence"] = {
        "implementation_dependencies": [candidate["evidence"]["implementation_dependencies"][1]]
    }
    candidate_path = root / "candidate.json"
    canonical_path = root / "canonical.json"
    replay_path = root / "replay.json"
    candidate_path.write_text(json.dumps(candidate, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    canonical_path.write_text(json.dumps(canonical, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    replay_path.write_text(
        json.dumps(
            {
                "status": "GOLDEN_REPLAY_MISMATCH_NOT_FROZEN",
                "summary": {"mismatched_count": 1},
                "capabilities": [
                    {
                        "capability_id": "connectors_and_capture",
                        "status": "mismatch",
                        "differing_fields": ["evidence"],
                        "dependency_digest_drifts": [
                            {
                                "path": "implementation_dependencies/observer",
                                "expected": "sha256:" + "1" * 64,
                                "observed": "sha256:" + "2" * 64,
                            }
                        ],
                    }
                ],
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    queue_path = root / "queue.json"
    build_review_queue(
        replay_report_path=replay_path,
        candidate_path=candidate_path,
        canonical_path=canonical_path,
        output_path=queue_path,
    )
    queue = json.loads(queue_path.read_text(encoding="utf-8"))
    return queue_path, canonical_path, str(queue["items"][0]["review_id"]), observation
