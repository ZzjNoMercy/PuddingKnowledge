from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import pytest

from knowledge_platform.continuity import (
    CapabilityTrafficController,
    CapabilityTrafficDispatcher,
    SqliteTrafficPolicyStore,
    TrafficPolicyError,
)


def test_policy_survives_controller_restart_and_failure_cutback(tmp_path) -> None:
    store = SqliteTrafficPolicyStore(tmp_path / "traffic.sqlite3")
    first = CapabilityTrafficController(store=store)
    first.configure(
        capability="wiki_query",
        deployment_revision="platform-v1",
        enabled=True,
        traffic_percent=100,
        rollback_window=timedelta(minutes=5),
    )

    restarted = CapabilityTrafficController(store=SqliteTrafficPolicyStore(tmp_path / "traffic.sqlite3"))
    assert restarted.decide(
        capability="wiki_query", routing_key="request-1", deployment_revision="platform-v1"
    ).route == "platform"
    restarted.record_failure(capability="wiki_query", deployment_revision="platform-v1", detail="probe failure")

    restarted_again = CapabilityTrafficController(store=SqliteTrafficPolicyStore(tmp_path / "traffic.sqlite3"))
    decision = restarted_again.decide(
        capability="wiki_query", routing_key="request-2", deployment_revision="platform-v1"
    )
    assert decision.route == "legacy"
    assert decision.reason == "unhealthy"
    assert "probe failure" not in repr(restarted_again.events)
    assert "traffic_cut_back_to_legacy" in SqliteTrafficPolicyStore(tmp_path / "traffic.sqlite3").event_types()


def test_durable_dispatcher_restarts_with_legacy_fallback(tmp_path) -> None:
    path = tmp_path / "traffic.sqlite3"
    controller = CapabilityTrafficController(store=SqliteTrafficPolicyStore(path))
    controller.configure(
        capability="table_query",
        deployment_revision="platform-v1",
        enabled=True,
        traffic_percent=100,
        rollback_window=timedelta(minutes=5),
    )
    dispatcher = CapabilityTrafficDispatcher(controller)
    first = dispatcher.dispatch(
        capability="table_query",
        routing_key="request-1",
        deployment_revision="platform-v1",
        request="request",
        platform_handler=lambda _request: "platform",
        legacy_handler=lambda _request: "legacy",
    )
    assert first.result == "platform"
    controller.record_failure(capability="table_query", deployment_revision="platform-v1", detail="controlled failure")

    restarted = CapabilityTrafficDispatcher(
        CapabilityTrafficController(store=SqliteTrafficPolicyStore(path))
    )
    second = restarted.dispatch(
        capability="table_query",
        routing_key="request-2",
        deployment_revision="platform-v1",
        request="request",
        platform_handler=lambda _request: "must-not-run",
        legacy_handler=lambda _request: "legacy-after-restart",
    )
    assert second.result == "legacy-after-restart"
    assert second.fallback is False
    assert second.decision.reason == "unhealthy"


def test_invalid_stored_policy_and_symlink_are_rejected(tmp_path) -> None:
    path = tmp_path / "traffic.sqlite3"
    store = SqliteTrafficPolicyStore(path)
    controller = CapabilityTrafficController(store=store)
    controller.configure(
        capability="wiki_query",
        deployment_revision="platform-v1",
        enabled=True,
        traffic_percent=1,
        rollback_window=timedelta(minutes=5),
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE platform_traffic_policies SET rollback_deadline = ? WHERE capability = ?",
            ("not-a-date", "wiki_query"),
        )
    with pytest.raises(TrafficPolicyError, match="stored traffic policy"):
        store.load("wiki_query")
    symlink = tmp_path / "traffic-link.sqlite3"
    symlink.symlink_to(path)
    with pytest.raises(OSError):
        SqliteTrafficPolicyStore(symlink)


def test_policy_snapshot_rejects_naive_deadline() -> None:
    from knowledge_platform.continuity import TrafficPolicySnapshot

    with pytest.raises(TrafficPolicyError, match="timezone-aware"):
        TrafficPolicySnapshot(
            capability="wiki_query",
            deployment_revision="platform-v1",
            enabled=True,
            traffic_percent=1,
            healthy=True,
            rollback_deadline=datetime(2026, 9, 4),
        )
