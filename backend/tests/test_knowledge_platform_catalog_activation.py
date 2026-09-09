from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from knowledge_platform.catalog.activation import (
    CatalogActivationController,
    CatalogActivationError,
    CatalogActivationState,
)
from knowledge_platform.catalog.activation_sqlite import SqliteCatalogActivationStore


def _digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def _controller(tmp_path: Path) -> CatalogActivationController:
    return CatalogActivationController(store=SqliteCatalogActivationStore(tmp_path / "activation.sqlite3"))


def _prepare_and_drain(controller: CatalogActivationController) -> tuple[str, str]:
    source = _digest("source")
    target = _digest("target")
    controller.prepare(
        installation_id="install-1",
        source_revision="legacy-v1",
        target_revision="platform-v1",
        source_digest=source,
        target_digest=target,
    )
    controller.mark_drained(proof_digest=_digest("drain"))
    return source, target


def test_activation_requires_order_and_restart_preserves_activated_state(tmp_path: Path) -> None:
    controller = _controller(tmp_path)
    source, target = _prepare_and_drain(controller)
    with pytest.raises(CatalogActivationError, match="verification"):
        controller.activate(deployment_revision="platform-v1")
    controller.verify(
        source_before_digest=source,
        source_after_digest=source,
        target_digest=target,
        expected_target_digest=target,
        checks={"source_unchanged": True, "target_digest_matches": True, "catalog_integrity": True},
    )
    restarted_verified = _controller(tmp_path)
    assert restarted_verified.state.status == "verified"
    assert restarted_verified.state.source_digest == source
    assert restarted_verified.state.target_digest == target
    assert restarted_verified.activate(deployment_revision="platform-v1").active_revision == "platform-v1"
    restarted = _controller(tmp_path)
    assert restarted.state.status == "activated"
    assert restarted.state.source_read_only is True
    assert SqliteCatalogActivationStore(tmp_path / "activation.sqlite3").event_types() == (
        "activation_prepared",
        "activation_drained",
        "activation_verified",
        "activation_committed",
    )


def test_verification_mismatch_and_incomplete_checks_fail_closed(tmp_path: Path) -> None:
    controller = _controller(tmp_path)
    source, target = _prepare_and_drain(controller)
    with pytest.raises(CatalogActivationError, match="verification failed"):
        controller.verify(
            source_before_digest=source,
            source_after_digest=_digest("changed"),
            target_digest=target,
            expected_target_digest=target,
            checks={"source_unchanged": True, "target_digest_matches": True, "catalog_integrity": True},
        )
    with pytest.raises(CatalogActivationError, match="incomplete"):
        controller.verify(
            source_before_digest=source,
            source_after_digest=source,
            target_digest=target,
            expected_target_digest=target,
            checks={"source_unchanged": True},
        )


def test_rollback_restores_legacy_and_keeps_target_read_only(tmp_path: Path) -> None:
    controller = _controller(tmp_path)
    source, target = _prepare_and_drain(controller)
    controller.verify(
        source_before_digest=source,
        source_after_digest=source,
        target_digest=target,
        expected_target_digest=target,
        checks={"source_unchanged": True, "target_digest_matches": True, "catalog_integrity": True},
    )
    controller.activate(deployment_revision="platform-v1")
    rollback = controller.rollback(reason="local failure")
    assert rollback.status == "rolled_back"
    assert rollback.active_revision == "legacy-v1"
    assert rollback.source_read_only is False
    assert rollback.target_read_only is True


def test_activation_store_rejects_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target.sqlite3"
    SqliteCatalogActivationStore(target)
    link = tmp_path / "link.sqlite3"
    link.symlink_to(target)
    with pytest.raises(OSError):
        SqliteCatalogActivationStore(link)


def test_state_rejects_non_boolean_pending_read_only_flags() -> None:
    with pytest.raises(ValueError, match="read-only flags"):
        CatalogActivationState("", "", "", "", "", "", "pending", 0, False)
