"""Build a path-free index of verified local migration evidence.

The bundle intentionally contains report digests and selected facts rather
than copying report payloads.  It is a navigation and audit artifact for the
current checkout, not a release manifest or an activation decision.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

_FORMAT = "agent-knowledge-platform-evidence-bundle/v1"
_STATUS = "LOCAL_EVIDENCE_BUNDLE_PASS_NOT_ACTIVATABLE"
_REPORTS = (
    ("phase8_capability_matrix", "phase8-local-capability-matrix-shadow-report.json"),
    ("phase9_distribution_matrix", "phase9-local-distribution-matrix-shadow-report.json"),
    ("phase10_package_build", "phase10-package-build-shadow.json"),
    ("phase10_dependency_sbom", "phase10-dependency-sbom-shadow.json"),
    ("phase10_extraction_preflight", "phase10-extraction-preflight.json"),
    ("phase10_rc_validation", "phase10-rc-validation-shadow.json"),
    ("phase10_installation_migration", "phase10-local-installation-migration-real-shadow.json"),
    ("phase_gate", "phase-gates-latest.json"),
)
_FORBIDDEN_MARKERS = ("/Users/", "/private/", "file://", "password=", "secret=")
_SAFE_REVISION = re.compile(r"^[A-Za-z0-9._:/+@-]+$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")


class EvidenceBundleError(ValueError):
    """Raised when a local evidence bundle cannot be safely assembled."""


def _sha256(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _validate_source_revision(source_revision: str) -> str:
    if not isinstance(source_revision, str) or not source_revision or len(source_revision) > 256:
        raise EvidenceBundleError("source revision is invalid")
    if not _SAFE_REVISION.fullmatch(source_revision) or any(
        marker in source_revision for marker in _FORBIDDEN_MARKERS
    ):
        raise EvidenceBundleError("source revision contains a forbidden marker")
    return source_revision


def _load_report(root: Path, relative: str) -> tuple[dict[str, Any], str]:
    path = root / relative
    try:
        if path.is_symlink() or not path.is_file():
            raise EvidenceBundleError(f"evidence report is unavailable: {relative}")
        raw = path.read_bytes()
        report = json.loads(raw.decode("utf-8"))
    except EvidenceBundleError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EvidenceBundleError(f"evidence report is unreadable: {relative}") from error
    if not isinstance(report, dict):
        raise EvidenceBundleError(f"evidence report must be an object: {relative}")
    serialized = json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if any(marker in serialized for marker in _FORBIDDEN_MARKERS):
        raise EvidenceBundleError(f"evidence report contains a forbidden marker: {relative}")
    return report, _sha256(raw)


def _facts(logical_name: str, report: dict[str, Any]) -> dict[str, Any]:
    if logical_name == "phase_gate":
        phases = report.get("phases")
        if not isinstance(phases, list):
            raise EvidenceBundleError("phase gate report has no phase list")
        phase_ready = {
            str(item["phase_id"]): item["ready"]
            for item in phases
            if isinstance(item, dict) and isinstance(item.get("phase_id"), str) and isinstance(item.get("ready"), bool)
        }
        if len(phase_ready) != len(phases):
            raise EvidenceBundleError("phase gate report has invalid phase entries")
        return {"blocker_count": len(report.get("blockers", [])), "phase_ready": phase_ready, "ready": report.get("ready")}
    keys_by_report = {
        "phase8_capability_matrix": ("capability_count", "pass_count", "independent_process_count", "clean_shutdown_count", "network_contacted", "external_network_contacted"),
        "phase9_distribution_matrix": ("check_count", "pass_count", "activation_allowed", "release_execution_allowed", "network_contacted", "independent_repositories_created"),
        "phase10_package_build": ("command_count", "all_commands_passed", "replay_consistent", "activation_allowed", "release_execution_allowed", "network_contacted"),
        "phase10_dependency_sbom": ("python_component_count", "node_component_count", "replay_consistent", "network_contacted", "dependency_install_performed", "release_artifact_generated"),
        "phase10_extraction_preflight": ("replay_consistent", "executable", "activation_allowed", "source_tag_signed", "source_worktree_clean", "missing_mixed_file_paths"),
        "phase10_rc_validation": ("releaseable", "release_rollback_window_open"),
        "phase10_installation_migration": (
            "replay_idempotent",
            "failure_recovery_verified",
            "partial_target_import_replay_verified",
            "post_cutover_delta_verified",
            "credential_rebind_recovery_verified",
            "rollback_reconciliation_verified",
            "stateful_rollback_replay_verified",
            "canonical_catalog_changed",
            "physical_copy_performed",
            "secret_bytes_read",
        ),
    }
    keys = keys_by_report.get(logical_name)
    if keys is None:
        raise EvidenceBundleError(f"unknown evidence report: {logical_name}")
    result = {key: report[key] for key in keys if key in report}
    if logical_name == "phase10_installation_migration":
        snapshot = report.get("snapshot")
        if not isinstance(snapshot, dict) or snapshot.get("source_manifest_verified") is not True:
            raise EvidenceBundleError("installation evidence has no verified source manifest")
        result["source_manifest_verified"] = True
        for key in ("source_manifest_digest", "snapshot_digest"):
            value = snapshot.get(key)
            if not isinstance(value, str) or not _SHA256.fullmatch(value):
                raise EvidenceBundleError(f"installation evidence has invalid {key}")
            result[key] = value
    if "status" in report:
        result["status"] = report["status"]
    return result


def _bundle_once(*, repo_root: Path, source_revision: str) -> dict[str, Any]:
    reports_root = repo_root / "artifacts/phase0b-local-catalog"
    entries = []
    for logical_name, relative in _REPORTS:
        report, digest = _load_report(reports_root, relative)
        entries.append(
            {
                "facts": _facts(logical_name, report),
                "logical_name": logical_name,
                "report_digest": digest,
            }
        )
    return {
        "format": _FORMAT,
        "status": _STATUS,
        "activation_allowed": False,
        "execution_allowed": False,
        "source_repository": "PuddingClaw",
        "source_revision": source_revision,
        "reports": entries,
        "report_count": len(entries),
        "network_contacted": False,
        "source_paths_emitted": False,
        "scope": "path-free index of same-checkout local shadow evidence; not an independent release or activation proof",
    }


def build_evidence_bundle(*, repo_root: Path, source_revision: str) -> dict[str, Any]:
    repo_root = repo_root.expanduser().resolve()
    source_revision = _validate_source_revision(source_revision)
    first = _bundle_once(repo_root=repo_root, source_revision=source_revision)
    second = _bundle_once(repo_root=repo_root, source_revision=source_revision)
    if first != second:
        raise EvidenceBundleError("evidence reports changed during bundle replay")
    first["replay_consistent"] = True
    first["bundle_digest"] = _sha256(
        json.dumps(first, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    return first


__all__ = ["EvidenceBundleError", "build_evidence_bundle"]
