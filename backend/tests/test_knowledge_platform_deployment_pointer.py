from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from knowledge_platform.catalog.deployment import (
    DeploymentActivationController,
    DeploymentActivationError,
    DeploymentArtifact,
    DeploymentManifest,
)
from knowledge_platform.catalog.deployment_sqlite import SqliteDeploymentActivationStore


def _digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def _manifest(revision: str) -> DeploymentManifest:
    return DeploymentManifest(
        revision,
        tuple(
            DeploymentArtifact(kind, revision, f"local-{kind}", _digest(f"{revision}:{kind}"))
            for kind in ("catalog", "blob", "vector_index", "wiki_root")
        ),
    )


def _controller(tmp_path: Path) -> DeploymentActivationController:
    return DeploymentActivationController(store=SqliteDeploymentActivationStore(tmp_path / "activation.sqlite3"))


def _prepare_and_drain(controller: DeploymentActivationController) -> tuple[DeploymentManifest, DeploymentManifest]:
    legacy = _manifest("legacy-v1")
    candidate = _manifest("platform-v1")
    controller.prepare(installation_id="install-1", legacy_manifest=legacy, candidate_manifest=candidate)
    controller.mark_drained(proof_digest=_digest("drain"))
    return legacy, candidate


def test_single_pointer_verifies_bundle_and_survives_restart(tmp_path: Path) -> None:
    controller = _controller(tmp_path)
    legacy, candidate = _prepare_and_drain(controller)
    controller.verify(
        legacy_before_digest=legacy.manifest_digest(),
        legacy_after_digest=legacy.manifest_digest(),
        candidate_manifest_digest=candidate.manifest_digest(),
        checks={"legacy_unchanged": True, "candidate_manifest_matches": True, "bundle_integrity": True},
    )
    restarted = _controller(tmp_path)
    assert restarted.state.status == "verified"
    assert restarted.state.installation_id == "install-1"
    assert restarted.read_context().manifest_digest() == legacy.manifest_digest()
    assert restarted.activate(deployment_revision="platform-v1").active_deployment_revision == "platform-v1"
    assert restarted.read_context().deployment_revision == "platform-v1"
    assert SqliteDeploymentActivationStore(tmp_path / "activation.sqlite3").event_types() == (
        "deployment_activation_prepared",
        "deployment_activation_drained",
        "deployment_activation_verified",
        "deployment_activation_committed",
    )


def test_bundle_requires_all_four_same_revision_and_stale_reads_fail(tmp_path: Path) -> None:
    legacy = _manifest("legacy-v1")
    artifacts = list(_manifest("platform-v1").artifacts)
    artifacts.pop()
    with pytest.raises(ValueError, match="exactly four"):
        DeploymentManifest("platform-v1", tuple(artifacts))
    with pytest.raises(DeploymentActivationError, match="stale"):
        controller = _controller(tmp_path)
        controller.prepare(installation_id="install-1", legacy_manifest=legacy, candidate_manifest=_manifest("platform-v1"))
        controller.assert_revision("platform-v1")


def test_verification_mismatch_and_rollback_fail_closed(tmp_path: Path) -> None:
    controller = _controller(tmp_path)
    legacy, candidate = _prepare_and_drain(controller)
    with pytest.raises(DeploymentActivationError, match="verification failed"):
        controller.verify(
            legacy_before_digest=legacy.manifest_digest(),
            legacy_after_digest=_digest("changed"),
            candidate_manifest_digest=candidate.manifest_digest(),
            checks={"legacy_unchanged": True, "candidate_manifest_matches": True, "bundle_integrity": True},
        )
    controller.rollback(reason="local failure")
    assert controller.state.active_deployment_revision == "legacy-v1"
    assert controller.state.candidate_read_only is True


def test_store_rejects_symlink_parent(tmp_path: Path) -> None:
    real_parent = tmp_path / "real"
    real_parent.mkdir()
    link_parent = tmp_path / "link"
    link_parent.symlink_to(real_parent, target_is_directory=True)
    with pytest.raises(OSError):
        SqliteDeploymentActivationStore(link_parent / "activation.sqlite3")
