from __future__ import annotations

import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from knowledge_platform.readiness import evaluate_phase_gates, load_phase_gate_manifest

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT.parent / "docs/knowledge-platform/phase-gate-evidence.yaml"


def test_current_manifest_is_explicitly_blocked_before_phase_1() -> None:
    report = evaluate_phase_gates(MANIFEST)

    assert report.ready is False
    assert {phase.phase_id for phase in report.phases if phase.ready} == {"phase_0c"}
    runtime_check = next(check for check in report.checks if check["check_id"] == "phase0a_runtime_probe_registry")
    assert runtime_check["returncode"] == 1
    assert "phase_0a.golden_baseline_frozen is blocked" in report.blockers
    assert "phase_0b.production_catalog_copy_verified is blocked" in report.blockers
    assert "phase_1 depends on phase_0a exit" in report.blockers


def _write_temp_manifest(document: dict, tmp_path: Path) -> Path:
    manifest = tmp_path / "docs/knowledge-platform/phase-gate-evidence.yaml"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(yaml.safe_dump(document), encoding="utf-8")
    return manifest


def _block_all_requirements(document: dict) -> None:
    for phase in document["phases"].values():
        for requirement in phase.values():
            requirement["status"] = "blocked"
            requirement.pop("check_id", None)
            requirement.pop("evidence_sha256", None)


def test_verified_requirement_requires_existing_local_evidence(tmp_path: Path) -> None:
    document = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    _block_all_requirements(document)
    document["phases"]["phase_0a"]["file_inventory_complete"] = {
        "status": "verified",
        "evidence_refs": ["missing-evidence.txt"],
        "evidence_sha256": {"missing-evidence.txt": "0" * 64},
        "check_id": "phase0a_dependency_inventory",
    }
    path = _write_temp_manifest(document, tmp_path)

    with pytest.raises(ValueError, match="evidence ref does not exist"):
        load_phase_gate_manifest(path)


def test_unknown_status_and_missing_check_id_fail_closed(tmp_path: Path) -> None:
    document = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    _block_all_requirements(document)
    document["phases"]["phase_0a"]["golden_baseline_frozen"]["status"] = "done"
    path = _write_temp_manifest(document, tmp_path)
    with pytest.raises(ValueError, match="must be verified or blocked"):
        load_phase_gate_manifest(path)

    evidence = tmp_path / "evidence.txt"
    evidence.write_text("reviewed\n", encoding="utf-8")
    document["phases"]["phase_0a"]["file_inventory_complete"] = {
        "status": "verified",
        "evidence_refs": ["evidence.txt"],
        "evidence_sha256": {"evidence.txt": "0" * 64},
    }
    manifest = _write_temp_manifest(document, tmp_path)
    with pytest.raises(ValueError, match="must use registered check"):
        load_phase_gate_manifest(manifest)


def test_phase_0c_scope_is_machine_checked(tmp_path: Path) -> None:
    document = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    _block_all_requirements(document)
    document["policy"]["phase_scopes"]["phase_0c"] = "production-cutover"
    with pytest.raises(ValueError, match="phase_scopes.phase_0c"):
        load_phase_gate_manifest(_write_temp_manifest(document, tmp_path))


def test_unexpected_requirement_fields_fail_closed(tmp_path: Path) -> None:
    document = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    _block_all_requirements(document)
    document["phases"]["phase_0a"]["file_inventory_complete"]["operator_override"] = True
    path = _write_temp_manifest(document, tmp_path)

    with pytest.raises(ValueError, match="unexpected fields"):
        load_phase_gate_manifest(path)


def test_phase1_cannot_be_hand_marked_verified_without_registered_evidence_check(tmp_path: Path) -> None:
    document = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    evidence = tmp_path / "evidence.txt"
    evidence.write_text("reviewed\n", encoding="utf-8")
    # The evaluator accepts only repository-local refs.  Put the fixture under
    # a repository-shaped temporary root and evaluate a manifest there.
    _block_all_requirements(document)
    for requirement in document["phases"]["phase_1"].values():
        requirement["status"] = "verified"
        requirement["evidence_refs"] = ["evidence.txt"]
        requirement["evidence_sha256"] = {"evidence.txt": "0" * 64}
    manifest = _write_temp_manifest(document, tmp_path)

    with pytest.raises(ValueError, match="must use registered check"):
        load_phase_gate_manifest(manifest)


def test_duplicate_yaml_keys_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "docs/knowledge-platform/phase-gate-evidence.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(
        "format: agent-knowledge-platform-phase-gate-evidence/v1\n"
        "spec_revision: v0.7\n"
        "format: forged\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate YAML key"):
        load_phase_gate_manifest(path)


def test_duplicate_evidence_refs_and_unsupported_spec_fail_closed(tmp_path: Path) -> None:
    document = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    _block_all_requirements(document)
    document["spec_revision"] = "v999"
    path = _write_temp_manifest(document, tmp_path)
    with pytest.raises(ValueError, match="spec_revision must equal v0.7"):
        load_phase_gate_manifest(path)

    document["spec_revision"] = "v0.7"
    document["phases"]["phase_0a"]["file_inventory_complete"]["evidence_refs"] = ["same.txt", "same.txt"]
    path = _write_temp_manifest(document, tmp_path)
    with pytest.raises(ValueError, match="must not contain duplicates"):
        load_phase_gate_manifest(path)


def test_changed_evidence_content_blocks_readiness(tmp_path: Path) -> None:
    document = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    _block_all_requirements(document)
    source = ROOT.parent
    for reference in (
        "docs/knowledge-platform/phase-0a-inventory.yaml",
        "backend/tests/test_phase0a_dependency_inventory.py",
    ):
        target = tmp_path / reference
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / reference, target)
    document["phases"]["phase_0a"]["file_inventory_complete"] = yaml.safe_load(
        yaml.safe_dump(yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))["phases"]["phase_0a"]["file_inventory_complete"])
    )
    document["phases"]["phase_0a"]["file_inventory_complete"]["evidence_sha256"][
        "docs/knowledge-platform/phase-0a-inventory.yaml"
    ] = "0" * 64
    report = evaluate_phase_gates(_write_temp_manifest(document, tmp_path), run_checks=False)
    assert any("evidence hash mismatch" in blocker for blocker in report.blockers)


def test_symlinked_evidence_is_rejected(tmp_path: Path) -> None:
    document = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    _block_all_requirements(document)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    link = tmp_path / "evidence-link.txt"
    link.symlink_to(outside)
    document["phases"]["phase_0a"]["file_inventory_complete"] = {
        "status": "verified",
        "evidence_refs": ["evidence-link.txt"],
        "evidence_sha256": {"evidence-link.txt": "0" * 64},
        "check_id": "phase0a_dependency_inventory",
    }
    with pytest.raises(ValueError, match="escaping evidence ref"):
        load_phase_gate_manifest(_write_temp_manifest(document, tmp_path))


def test_cli_returns_failure_for_blocked_report_even_without_require_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    from knowledge_platform.readiness import phase_gates

    monkeypatch.setattr(sys, "argv", ["phase_gates", "--manifest", str(MANIFEST)])
    monkeypatch.setattr(
        phase_gates,
        "evaluate_phase_gates",
        lambda path: SimpleNamespace(ready=False, to_dict=lambda: {"ready": False}),
    )
    assert phase_gates.main() == 1


def test_registered_check_failure_is_a_readiness_blocker() -> None:
    from knowledge_platform.readiness import phase_gates

    def failed_runner(*args: object, **kwargs: object) -> object:
        del args, kwargs
        return phase_gates.subprocess.CompletedProcess([], 1, "failed", "error")

    report = evaluate_phase_gates(MANIFEST, check_runner=failed_runner)
    assert any("registered check failed" in blocker for blocker in report.blockers)
