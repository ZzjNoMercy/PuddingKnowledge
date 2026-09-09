from __future__ import annotations

from datetime import timedelta

import pytest

from knowledge_platform.continuity import (
    CUTOVER_UNIT_CAPABILITIES,
    CapabilityCutoverCoordinator,
    CapabilityTrafficController,
    SqliteCutoverStateStore,
    SqliteTrafficPolicyStore,
)


def _coordinator(tmp_path):
    traffic = CapabilityTrafficController(store=SqliteTrafficPolicyStore(tmp_path / "traffic.sqlite3"))
    return CapabilityCutoverCoordinator(
        traffic=traffic,
        store=SqliteCutoverStateStore(tmp_path / "cutover.sqlite3"),
    )


def _promote(coordinator, unit: str) -> None:
    coordinator.promote(
        unit=unit,
        deployment_revision="platform-v1",
        traffic_percent=100,
        rollback_window=timedelta(minutes=5),
    )


def test_cutover_order_is_document_wiki_table_database(tmp_path) -> None:
    coordinator = _coordinator(tmp_path)

    _promote(coordinator, "document")
    with pytest.raises(ValueError, match="health proof is invalid"):
        coordinator.stabilize(unit="document", health_proof="")
    coordinator.stabilize(unit="document", health_proof="document-health")
    with pytest.raises(Exception, match="previous cutover unit"):
        coordinator.promote(
            unit="table",
            deployment_revision="platform-v1",
            traffic_percent=100,
            rollback_window=timedelta(minutes=5),
        )

    _promote(coordinator, "wiki")
    coordinator.stabilize(unit="wiki", health_proof="wiki-health")
    _promote(coordinator, "table")
    coordinator.stabilize(unit="table", health_proof="table-health")
    _promote(coordinator, "database")

    assert coordinator.state("database").status == "active"
    assert CUTOVER_UNIT_CAPABILITIES["database"] == ("database_nl2sql_execute_readonly",)


def test_database_stages_cannot_be_registered_as_separate_cutover_capabilities(tmp_path) -> None:
    coordinator = _coordinator(tmp_path)
    with pytest.raises(Exception, match="not part of the cutover plan"):
        coordinator.dispatch(
            capability="database_nl2sql",
            routing_key="q-1",
            deployment_revision="platform-v1",
            request="request",
            platform_handler=lambda _request: "platform",
            legacy_handler=lambda _request: "legacy",
        )


def test_restart_preserves_unit_state_and_rollback_disables_later_units(tmp_path) -> None:
    coordinator = _coordinator(tmp_path)
    _promote(coordinator, "document")
    coordinator.stabilize(unit="document", health_proof="document-health")
    _promote(coordinator, "wiki")
    coordinator.stabilize(unit="wiki", health_proof="wiki-health")

    restarted = _coordinator(tmp_path)
    assert restarted.state("wiki").status == "stable"
    _promote(restarted, "table")
    result = restarted.dispatch(
        capability="table_query",
        routing_key="table-1",
        deployment_revision="platform-v1",
        request="request",
        platform_handler=lambda _request: (_ for _ in ()).throw(RuntimeError("failure")),
        legacy_handler=lambda _request: "legacy",
    )

    assert result.result == "legacy"
    assert result.fallback is True
    assert restarted.state("table").status == "rolled_back"
    assert restarted.state("database").status == "pending"
    restored = _coordinator(tmp_path)
    assert restored.state("table").status == "rolled_back"
    assert "cutover_rolled_back" in SqliteCutoverStateStore(tmp_path / "cutover.sqlite3").event_types()


def test_pending_unit_dispatches_legacy_without_touching_platform(tmp_path) -> None:
    coordinator = _coordinator(tmp_path)
    calls: list[str] = []
    result = coordinator.dispatch(
        capability="wiki_query",
        routing_key="wiki-1",
        deployment_revision="platform-v1",
        request="request",
        platform_handler=lambda _request: calls.append("platform") or "platform",
        legacy_handler=lambda _request: calls.append("legacy") or "legacy",
    )

    assert result.result == "legacy"
    assert result.decision.reason == "unit_not_promoted"
    assert calls == ["legacy"]
