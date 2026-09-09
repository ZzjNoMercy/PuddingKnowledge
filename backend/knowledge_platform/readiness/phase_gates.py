"""Evidence-backed, fail-closed phase gate evaluation.

The evaluator intentionally does not infer readiness from the presence of
source files or passing unit tests. A phase is ready only when its manifest
contains an explicit ``verified`` requirement, a fixed registered check, and
repository-local evidence references whose content hashes match. This makes
the artifact a reviewable control plane for the migration rather than another
optimistic progress counter.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

MANIFEST_FORMAT = "agent-knowledge-platform-phase-gate-evidence/v1"
_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SUPPORTED_SPEC_REVISION = "v0.7"
PHASE_0C_SCOPE = "provider-neutral-reference-only"

PHASE_REQUIREMENTS: dict[str, tuple[str, ...]] = {
    "phase_0a": (
        "file_inventory_complete",
        "golden_baseline_frozen",
        "source_snapshot_and_fixture_manifest_frozen",
        "runtime_dependency_graph_verified",
        "vanna_provenance_resolved",
    ),
    "phase_0b": (
        "target_catalogs_independently_bootable",
        "production_catalog_copy_verified",
        "real_vault_rebind_rotation_verified",
        "drain_copy_verify_rollback_verified",
    ),
    "phase_0c": (
        "static_dependency_boundary_verified",
        "framework_neutral_ports_verified",
        "protocol_v2_boundary_verified",
        "dynamic_delegation_boundary_verified",
        "deepagents_wiring_stop_order_recorded",
        "contracts_verified",
    ),
    "phase_1": (
        "organization_decisions_confirmed",
        "platform_application_service_extracted",
        "shadow_namespace_and_side_effect_fence_verified",
    ),
}

PHASE_DEPENDENCIES: dict[str, tuple[str, ...]] = {
    "phase_1": ("phase_0a", "phase_0b", "phase_0c"),
}

CHECK_COMMANDS: dict[str, tuple[str, ...]] = {
    "phase0a_dependency_inventory": (
        "backend/.venv/bin/pytest",
        "-q",
        "backend/tests/test_phase0a_dependency_inventory.py",
    ),
    "phase0a_runtime_probe_registry": (
        "backend/.venv/bin/python",
        "backend/scripts/phase0a_runtime_dependency_gate.py",
    ),
    "phase0a_vanna_provenance": (
        "backend/.venv/bin/python",
        "backend/scripts/vanna_vendor_patch_provenance.py",
    ),
    "phase0a_golden_fixtures": (
        "backend/.venv/bin/python",
        "backend/scripts/phase0a_fixture_manifest.py",
        "--require-frozen",
    ),
    "phase0b_catalog_bootstrap": (
        "backend/.venv/bin/pytest",
        "-q",
        "backend/tests/test_knowledge_platform_boundaries.py",
    ),
    "phase0c_static_boundary": (
        "backend/.venv/bin/pytest",
        "-q",
        "backend/tests/test_knowledge_platform_boundaries.py",
    ),
    "phase0c_contracts": (
        "backend/.venv/bin/pytest",
        "-q",
        "backend/tests/test_knowledge_contracts.py",
    ),
    "phase0c_framework_neutral_ports": (
        "backend/.venv/bin/pytest",
        "-q",
        "backend/tests/test_knowledge_platform_lease.py",
        "backend/tests/test_knowledge_platform_wiki_compiler.py",
        "backend/tests/test_knowledge_platform_research_delegation.py",
        "backend/tests/test_knowledge_platform_evidence_ports.py",
        "backend/tests/test_knowledge_platform_agent_surface.py",
    ),
    "phase0c_protocol_boundary": (
        "backend/.venv/bin/pytest",
        "-q",
        "backend/tests/test_knowledge_platform_protocol.py",
    ),
    "phase0c_dynamic_delegation": (
        "backend/.venv/bin/pytest",
        "-q",
        "backend/tests/test_knowledge_platform_research_delegation.py",
    ),
    "phase0c_wiring_stop_order": (
        "backend/.venv/bin/pytest",
        "-q",
        "backend/tests/test_knowledge_platform_agent_surface.py",
    ),
    "phase1_application_service": (
        "backend/.venv/bin/pytest",
        "-q",
        "backend/tests/test_knowledge_platform_catalog_query_service.py",
    ),
    "phase1_shadow_fence": (
        "backend/.venv/bin/pytest",
        "-q",
        "backend/tests/test_knowledge_platform_catalog_query_service.py",
        "backend/tests/test_phase1_local_catalog_shadow.py",
    ),
}

REQUIREMENT_CHECKS: dict[str, str] = {
    "phase_0a.file_inventory_complete": "phase0a_dependency_inventory",
    "phase_0a.runtime_dependency_graph_verified": "phase0a_runtime_probe_registry",
    "phase_0a.source_snapshot_and_fixture_manifest_frozen": "phase0a_golden_fixtures",
    "phase_0a.vanna_provenance_resolved": "phase0a_vanna_provenance",
    "phase_0b.target_catalogs_independently_bootable": "phase0b_catalog_bootstrap",
    "phase_0c.static_dependency_boundary_verified": "phase0c_static_boundary",
    "phase_0c.framework_neutral_ports_verified": "phase0c_framework_neutral_ports",
    "phase_0c.protocol_v2_boundary_verified": "phase0c_protocol_boundary",
    "phase_0c.dynamic_delegation_boundary_verified": "phase0c_dynamic_delegation",
    "phase_0c.deepagents_wiring_stop_order_recorded": "phase0c_wiring_stop_order",
    "phase_0c.contracts_verified": "phase0c_contracts",
    "phase_1.platform_application_service_extracted": "phase1_application_service",
    "phase_1.shadow_namespace_and_side_effect_fence_verified": "phase1_shadow_fence",
}


class GateStatus(StrEnum):
    VERIFIED = "verified"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class GateRequirement:
    """One explicit claim in the phase-gate manifest."""

    requirement_id: str
    status: GateStatus
    evidence_refs: tuple[str, ...]
    evidence_sha256: tuple[tuple[str, str], ...]
    check_id: str | None


@dataclass(frozen=True, slots=True)
class GateResult:
    phase_id: str
    ready: bool
    blockers: tuple[str, ...]
    requirements: tuple[GateRequirement, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase_id": self.phase_id,
            "ready": self.ready,
            "blockers": list(self.blockers),
            "requirements": [
                {
                    "requirement_id": requirement.requirement_id,
                    "status": requirement.status.value,
                    "evidence_refs": list(requirement.evidence_refs),
                    "evidence_sha256": dict(requirement.evidence_sha256),
                    "check_id": requirement.check_id,
                    "registered_command": list(CHECK_COMMANDS[requirement.check_id])
                    if requirement.check_id is not None
                    else None,
                }
                for requirement in self.requirements
            ],
        }


@dataclass(frozen=True, slots=True)
class PhaseReadinessReport:
    spec_revision: str
    manifest_path: str
    ready: bool
    phases: tuple[GateResult, ...]
    checks: tuple[dict[str, Any], ...]

    @property
    def blockers(self) -> tuple[str, ...]:
        return tuple(blocker for phase in self.phases for blocker in phase.blockers)

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": "agent-knowledge-platform-phase-gate-report/v1",
            "spec_revision": self.spec_revision,
            "manifest_path": self.manifest_path,
            "ready": self.ready,
            "blockers": list(self.blockers),
            "phases": [phase.to_dict() for phase in self.phases],
            "checks": list(self.checks),
        }


def _fail(message: str) -> ValueError:
    return ValueError(f"invalid phase-gate manifest: {message}")


def _require_mapping(value: object, *, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _fail(f"{field} must be a mapping")
    return value


def _require_nonempty_string(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _fail(f"{field} must be a non-empty string")
    return value.strip()


def _parse_requirement(
    requirement_id: str,
    raw_requirement: object,
    *,
    phase_id: str,
    repository_root: Path,
) -> GateRequirement:
    if not _ID_RE.fullmatch(requirement_id):
        raise _fail(f"{phase_id}.{requirement_id} is not a stable identifier")
    raw = _require_mapping(raw_requirement, field=f"{phase_id}.{requirement_id}")
    unexpected_fields = set(raw) - {"status", "evidence_refs", "evidence_sha256", "check_id"}
    if unexpected_fields:
        raise _fail(f"{phase_id}.{requirement_id} has unexpected fields: {sorted(unexpected_fields)}")
    status_value = _require_nonempty_string(raw.get("status"), field=f"{phase_id}.{requirement_id}.status")
    try:
        status = GateStatus(status_value)
    except ValueError as exc:
        raise _fail(f"{phase_id}.{requirement_id}.status must be verified or blocked") from exc

    raw_refs = raw.get("evidence_refs", [])
    if not isinstance(raw_refs, list) or any(not isinstance(ref, str) or not ref.strip() for ref in raw_refs):
        raise _fail(f"{phase_id}.{requirement_id}.evidence_refs must be a list of non-empty strings")
    evidence_refs = tuple(ref.strip() for ref in raw_refs)
    if len(set(evidence_refs)) != len(evidence_refs):
        raise _fail(f"{phase_id}.{requirement_id}.evidence_refs must not contain duplicates")
    raw_hashes = raw.get("evidence_sha256", {})
    if not isinstance(raw_hashes, dict) or any(
        not isinstance(ref, str) or not isinstance(digest, str) or not ref.strip() or not digest.strip()
        for ref, digest in raw_hashes.items()
    ):
        raise _fail(f"{phase_id}.{requirement_id}.evidence_sha256 must map non-empty refs to digests")
    evidence_sha256 = tuple(sorted((ref.strip(), digest.strip().lower()) for ref, digest in raw_hashes.items()))
    check_id = raw.get("check_id")
    if check_id is not None and (not isinstance(check_id, str) or not check_id.strip()):
        raise _fail(f"{phase_id}.{requirement_id}.check_id must be null or a non-empty string")
    check_id = check_id.strip() if isinstance(check_id, str) else None
    expected_check = REQUIREMENT_CHECKS.get(f"{phase_id}.{requirement_id}")

    if status is GateStatus.VERIFIED:
        if not evidence_refs:
            raise _fail(f"{phase_id}.{requirement_id} verified without evidence_refs")
        evidence_hashes = dict(evidence_sha256)
        if set(evidence_hashes) != set(evidence_refs):
            raise _fail(f"{phase_id}.{requirement_id} verified without an evidence hash for every ref")
        if any(not _SHA256_RE.fullmatch(digest) for digest in evidence_hashes.values()):
            raise _fail(f"{phase_id}.{requirement_id} contains an invalid SHA-256 evidence digest")
        if expected_check is None or check_id != expected_check:
            raise _fail(f"{phase_id}.{requirement_id} must use registered check {expected_check!r}")
        if check_id not in CHECK_COMMANDS:
            raise _fail(f"{phase_id}.{requirement_id} references unknown check {check_id!r}")
    elif check_id is not None or evidence_sha256:
        raise _fail(f"{phase_id}.{requirement_id} blocked must not carry check_id or evidence hashes")

    if status is GateStatus.VERIFIED:
        for reference in evidence_refs:
            reference_path = Path(reference)
            if reference_path.is_absolute() or ".." in reference_path.parts:
                raise _fail(f"{phase_id}.{requirement_id} has unsafe evidence ref {reference!r}")
            resolved_reference = (repository_root / reference_path).resolve()
            if not resolved_reference.is_relative_to(repository_root) or (repository_root / reference_path).is_symlink():
                raise _fail(f"{phase_id}.{requirement_id} has escaping evidence ref {reference!r}")
            if not resolved_reference.is_file():
                raise _fail(f"{phase_id}.{requirement_id} evidence ref does not exist: {reference}")

    return GateRequirement(requirement_id, status, evidence_refs, evidence_sha256, check_id)


def load_phase_gate_manifest(path: Path) -> tuple[str, dict[str, tuple[GateRequirement, ...]]]:
    """Load and structurally validate a phase-gate manifest."""

    repository_root = _repository_root_for_manifest(path)
    raw_document = _load_yaml_without_duplicate_keys(path)
    document = _require_mapping(raw_document, field="document")
    unexpected_document_fields = set(document) - {"format", "spec_revision", "policy", "phases"}
    if unexpected_document_fields:
        raise _fail(f"document has unexpected fields: {sorted(unexpected_document_fields)}")
    if document.get("format") != MANIFEST_FORMAT:
        raise _fail(f"format must equal {MANIFEST_FORMAT}")
    spec_revision = _require_nonempty_string(document.get("spec_revision"), field="spec_revision")
    if spec_revision != SUPPORTED_SPEC_REVISION:
        raise _fail(f"spec_revision must equal {SUPPORTED_SPEC_REVISION}")
    policy = _require_mapping(document.get("policy"), field="policy")
    phase_scopes = _require_mapping(policy.get("phase_scopes"), field="policy.phase_scopes")
    if phase_scopes.get("phase_0c") != PHASE_0C_SCOPE:
        raise _fail(f"policy.phase_scopes.phase_0c must equal {PHASE_0C_SCOPE}")
    raw_phases = _require_mapping(document.get("phases"), field="phases")
    if set(raw_phases) != set(PHASE_REQUIREMENTS):
        raise _fail("phases must contain exactly phase_0a, phase_0b, phase_0c, phase_1")

    phases: dict[str, tuple[GateRequirement, ...]] = {}
    for phase_id, required_ids in PHASE_REQUIREMENTS.items():
        raw_requirements = _require_mapping(raw_phases.get(phase_id), field=f"phases.{phase_id}")
        if set(raw_requirements) != set(required_ids):
            raise _fail(f"{phase_id} must contain exactly {', '.join(required_ids)}")
        phases[phase_id] = tuple(
            _parse_requirement(
                requirement_id,
                raw_requirements[requirement_id],
                phase_id=phase_id,
                repository_root=repository_root,
            )
            for requirement_id in required_ids
        )
    return spec_revision, phases


def _repository_root_for_manifest(path: Path) -> Path:
    resolved = path.resolve()
    if resolved.parent.name != "knowledge-platform" or resolved.parent.parent.name != "docs":
        raise _fail("manifest must be located under docs/knowledge-platform")
    return resolved.parent.parent.parent


class _UniqueKeySafeLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(loader: _UniqueKeySafeLoader, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise _fail(f"duplicate YAML key {key!r}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _load_yaml_without_duplicate_keys(path: Path) -> object:
    with path.open(encoding="utf-8") as stream:
        return yaml.load(stream, Loader=_UniqueKeySafeLoader)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run_registered_checks(
    requirements: tuple[GateRequirement, ...],
    *,
    repository_root: Path,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> tuple[dict[str, Any], ...]:
    # Run every registered requirement check, including checks belonging to
    # currently blocked claims. This keeps a blocked report evidence-backed and
    # prevents a future status flip from silently bypassing a registered gate.
    check_ids = sorted(
        {
            requirement.check_id for requirement in requirements if requirement.check_id is not None
        }
        | set(REQUIREMENT_CHECKS.values())
    )
    results: list[dict[str, Any]] = []
    for check_id in check_ids:
        assert check_id is not None
        command = CHECK_COMMANDS[check_id]
        try:
            completed = runner(
                command,
                cwd=repository_root,
                capture_output=True,
                text=True,
                check=False,
                timeout=300,
            )
            returncode = completed.returncode
            stdout = completed.stdout or ""
            stderr = completed.stderr or ""
            error = None
        except (OSError, subprocess.TimeoutExpired) as exc:
            returncode = 125
            stdout = ""
            stderr = ""
            error = type(exc).__name__
        results.append(
            {
                "check_id": check_id,
                "command": list(command),
                "returncode": returncode,
                "stdout_sha256": hashlib.sha256(stdout.encode()).hexdigest(),
                "stderr_sha256": hashlib.sha256(stderr.encode()).hexdigest(),
                "error": error,
            }
        )
    return tuple(results)


def evaluate_phase_gates(
    path: Path,
    *,
    run_checks: bool = True,
    check_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> PhaseReadinessReport:
    """Return readiness; a verified claim also needs a passing registered check and hash match."""

    spec_revision, phases = load_phase_gate_manifest(path)
    repository_root = _repository_root_for_manifest(path)
    all_requirements = tuple(requirement for phase in phases.values() for requirement in phase)
    checks = _run_registered_checks(all_requirements, repository_root=repository_root, runner=check_runner) if run_checks else ()
    check_by_id = {result["check_id"]: result for result in checks}
    results: dict[str, GateResult] = {}
    for phase_id, requirements in phases.items():
        blockers = [
            f"{phase_id}.{requirement.requirement_id} is {requirement.status.value}"
            for requirement in requirements
            if requirement.status is not GateStatus.VERIFIED
        ]
        for requirement in requirements:
            if requirement.status is not GateStatus.VERIFIED:
                continue
            for reference in requirement.evidence_refs:
                expected_digest = dict(requirement.evidence_sha256)[reference]
                actual_digest = _sha256(repository_root / reference)
                if actual_digest != expected_digest:
                    blockers.append(f"{phase_id}.{requirement.requirement_id} evidence hash mismatch: {reference}")
            if not run_checks:
                blockers.append(f"{phase_id}.{requirement.requirement_id} registered check not executed")
            elif check_by_id[requirement.check_id]["returncode"] != 0:
                blockers.append(f"{phase_id}.{requirement.requirement_id} registered check failed: {requirement.check_id}")
        for dependency in PHASE_DEPENDENCIES.get(phase_id, ()):
            if not results[dependency].ready:
                blockers.append(f"{phase_id} depends on {dependency} exit")
        results[phase_id] = GateResult(phase_id, not blockers, tuple(blockers), requirements)
    return PhaseReadinessReport(
        spec_revision,
        str(path),
        all(result.ready for result in results.values()),
        tuple(results.values()),
        checks,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--require-ready", action="store_true")
    args = parser.parse_args()
    report = evaluate_phase_gates(args.manifest)
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    # A readiness report is a gate, not an informational success signal.
    # Keep --require-ready as a self-documenting compatibility flag, but never
    # let omission of it turn a blocked report into a zero exit code.
    del args.require_ready
    return 0 if report.ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
