from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from knowledge_platform.continuity import (
    QUERY_TRAFFIC_CAPABILITIES,
    TRAFFIC_CAPABILITIES,
    CapabilityTrafficController,
    CapabilityTrafficDispatcher,
    TrafficPolicyError,
)


def _controller(now: list[datetime]) -> CapabilityTrafficController:
    return CapabilityTrafficController(clock=lambda: now[0])


def test_each_capability_can_be_enabled_at_a_bounded_percentage() -> None:
    controller = CapabilityTrafficController()
    controller.configure(
        capability="wiki_compile",
        deployment_revision="platform-v1",
        enabled=True,
        traffic_percent=100,
        rollback_window=timedelta(minutes=5),
    )

    decision = controller.decide(
        capability="wiki_compile", routing_key="asset-1", deployment_revision="platform-v1"
    )

    assert decision.route == "platform"
    assert decision.reason == "within_window"
    assert decision.rollback_window_open is True
    assert decision.key_digest.startswith("sha256:")
    assert "asset-1" not in repr(controller.events)


def test_default_policy_covers_query_units_and_keeps_database_stages_atomic() -> None:
    assert "wiki_query" in TRAFFIC_CAPABILITIES
    assert "database_nl2sql_execute_readonly" in QUERY_TRAFFIC_CAPABILITIES
    controller = CapabilityTrafficController()
    controller.configure(
        capability="database_nl2sql_execute_readonly",
        deployment_revision="platform-v1",
        enabled=True,
        traffic_percent=100,
        rollback_window=timedelta(minutes=5),
    )

    decision = controller.decide(
        capability="database_nl2sql_execute_readonly",
        routing_key="query-1",
        deployment_revision="platform-v1",
    )

    assert decision.route == "platform"


def test_dispatcher_executes_selected_handler_and_cuts_back_after_platform_failure() -> None:
    controller = CapabilityTrafficController()
    controller.configure(
        capability="wiki_query",
        deployment_revision="platform-v1",
        enabled=True,
        traffic_percent=100,
        rollback_window=timedelta(minutes=5),
    )
    dispatcher = CapabilityTrafficDispatcher(controller)
    calls: list[str] = []

    first = dispatcher.dispatch(
        capability="wiki_query",
        routing_key="request-1",
        deployment_revision="platform-v1",
        request={"query": "x"},
        platform_handler=lambda request: calls.append("platform") or "platform-result",
        legacy_handler=lambda request: calls.append("legacy") or "legacy-result",
    )
    second = dispatcher.dispatch(
        capability="wiki_query",
        routing_key="request-2",
        deployment_revision="platform-v1",
        request={"query": "x"},
        platform_handler=lambda request: calls.append("platform-failed") or (_ for _ in ()).throw(RuntimeError()),
        legacy_handler=lambda request: calls.append("legacy-after-failure") or "legacy-result",
    )

    assert first.result == "platform-result"
    assert first.fallback is False
    assert second.result == "legacy-result"
    assert second.fallback is True
    assert second.decision.reason == "unhealthy"
    assert calls == ["platform", "platform-failed", "legacy-after-failure"]


def test_dispatcher_uses_legacy_for_disabled_or_unconfigured_capability() -> None:
    controller = CapabilityTrafficController()
    dispatcher = CapabilityTrafficDispatcher(controller)
    result = dispatcher.dispatch(
        capability="table_query",
        routing_key="request-1",
        deployment_revision="platform-v1",
        request=None,
        platform_handler=lambda request: "must-not-run",
        legacy_handler=lambda request: "legacy-result",
    )

    assert result.result == "legacy-result"
    assert result.decision.route == "legacy"
    assert result.decision.reason == "not_configured"


def test_disabled_or_unknown_capability_fails_closed_to_legacy() -> None:
    controller = CapabilityTrafficController()
    controller.configure(
        capability="wiki_compile",
        deployment_revision="platform-v1",
        enabled=False,
        traffic_percent=100,
        rollback_window=timedelta(minutes=5),
    )

    assert controller.decide(
        capability="wiki_compile", routing_key="asset-1", deployment_revision="platform-v1"
    ).reason == "feature_disabled"
    assert controller.decide(
        capability="capture_processing", routing_key="capture-1", deployment_revision="platform-v1"
    ).reason == "not_configured"


def test_health_failure_cuts_one_capability_back_without_affecting_other() -> None:
    controller = CapabilityTrafficController()
    for capability in ("wiki_compile", "capture_processing"):
        controller.configure(
            capability=capability,
            deployment_revision="platform-v1",
            enabled=True,
            traffic_percent=100,
            rollback_window=timedelta(minutes=5),
        )

    controller.record_failure(capability="wiki_compile", deployment_revision="platform-v1", detail="probe failed")

    assert controller.decide(
        capability="wiki_compile", routing_key="asset-1", deployment_revision="platform-v1"
    ).reason == "unhealthy"
    assert controller.decide(
        capability="capture_processing", routing_key="capture-1", deployment_revision="platform-v1"
    ).route == "platform"
    assert "probe failed" not in repr(controller.events)


def test_expired_rollback_window_returns_legacy() -> None:
    now = [datetime(2026, 9, 4, tzinfo=UTC)]
    controller = _controller(now)
    controller.configure(
        capability="wiki_compile",
        deployment_revision="platform-v1",
        enabled=True,
        traffic_percent=100,
        rollback_window=timedelta(seconds=10),
    )
    now[0] += timedelta(seconds=10)

    decision = controller.decide(
        capability="wiki_compile", routing_key="asset-1", deployment_revision="platform-v1"
    )

    assert decision.route == "legacy"
    assert decision.reason == "rollback_window_expired"
    assert decision.rollback_window_open is False


def test_revision_change_and_malformed_policy_are_rejected() -> None:
    controller = CapabilityTrafficController()
    controller.configure(
        capability="wiki_compile",
        deployment_revision="platform-v1",
        enabled=True,
        traffic_percent=50,
        rollback_window=timedelta(minutes=5),
    )
    with pytest.raises(TrafficPolicyError, match="cannot change deployment revision"):
        controller.configure(
            capability="wiki_compile",
            deployment_revision="platform-v2",
            enabled=True,
            traffic_percent=50,
            rollback_window=timedelta(minutes=5),
        )
    with pytest.raises(TrafficPolicyError):
        controller.configure(
            capability="capture_processing",
            deployment_revision="platform-v1",
            enabled=True,
            traffic_percent=101,
            rollback_window=timedelta(minutes=5),
        )
    with pytest.raises(TrafficPolicyError):
        controller.decide(capability="wiki_compile", routing_key="asset-1", deployment_revision="platform-v2")


def test_inactive_deployment_revision_fails_closed_to_legacy() -> None:
    active = {"platform-v1"}
    controller = CapabilityTrafficController(active_revision_check=lambda revision: revision in active)
    controller.configure(
        capability="wiki_compile",
        deployment_revision="platform-v1",
        enabled=True,
        traffic_percent=100,
        rollback_window=timedelta(minutes=5),
    )

    active.remove("platform-v1")
    decision = controller.decide(
        capability="wiki_compile", routing_key="asset-1", deployment_revision="platform-v1"
    )

    assert decision.route == "legacy"
    assert decision.reason == "deployment_not_active"
    assert decision.rollback_window_open is False


def test_broken_active_revision_check_fails_closed_without_exposing_error() -> None:
    def broken_check(_revision: str) -> bool:
        raise RuntimeError("private deployment detail")

    controller = CapabilityTrafficController(active_revision_check=broken_check)
    controller.configure(
        capability="wiki_compile",
        deployment_revision="platform-v1",
        enabled=True,
        traffic_percent=100,
        rollback_window=timedelta(minutes=5),
    )

    decision = controller.decide(
        capability="wiki_compile", routing_key="asset-1", deployment_revision="platform-v1"
    )

    assert decision.route == "legacy"
    assert decision.reason == "deployment_not_active"
    assert "private deployment detail" not in repr(decision)


def test_naive_clock_and_unbounded_window_fail_closed() -> None:
    controller = CapabilityTrafficController(clock=lambda: datetime(2026, 9, 4))
    with pytest.raises(TrafficPolicyError, match="aware"):
        controller.configure(
            capability="wiki_compile",
            deployment_revision="platform-v1",
            enabled=True,
            traffic_percent=1,
            rollback_window=timedelta(minutes=5),
        )
    with pytest.raises(TrafficPolicyError):
        CapabilityTrafficController().configure(
            capability="wiki_compile",
            deployment_revision="platform-v1",
            enabled=True,
            traffic_percent=1,
            rollback_window=timedelta(days=8),
        )


def test_non_datetime_clock_and_non_string_failure_detail_are_rejected() -> None:
    controller = CapabilityTrafficController(clock=lambda: "not-a-clock")
    with pytest.raises(TrafficPolicyError, match="aware"):
        controller.configure(
            capability="wiki_compile",
            deployment_revision="platform-v1",
            enabled=True,
            traffic_percent=1,
            rollback_window=timedelta(minutes=5),
        )

    healthy_controller = CapabilityTrafficController()
    healthy_controller.configure(
        capability="wiki_compile",
        deployment_revision="platform-v1",
        enabled=True,
        traffic_percent=1,
        rollback_window=timedelta(minutes=5),
    )
    with pytest.raises(TrafficPolicyError, match="failure detail"):
        healthy_controller.record_failure(
            capability="wiki_compile", deployment_revision="platform-v1", detail=None  # type: ignore[arg-type]
        )
