"""Ordered, restartable Capability cutover control for local rehearsals."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Protocol

from .traffic import (
    CapabilityTrafficController,
    CapabilityTrafficDispatcher,
    TrafficDecision,
    TrafficDispatchResult,
    TrafficPolicyError,
)

_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,160}$")
CUTOVER_UNITS: tuple[str, ...] = ("document", "wiki", "table", "database")
CUTOVER_UNIT_CAPABILITIES: dict[str, tuple[str, ...]] = {
    "document": (
        "knowledge_list",
        "knowledge_search",
        "knowledge_read",
        "document_rag_query",
        "capture_processing",
        "connector_sync",
    ),
    "wiki": ("wiki_query", "wiki_compile", "gbrain_projection"),
    "table": ("table_query", "logical_dataset_processing", "semantic_dimension"),
    "database": ("database_nl2sql_execute_readonly",),
}
_CUTOVER_STATES = frozenset({"pending", "active", "stable", "rolled_back"})


@dataclass(frozen=True, slots=True)
class CutoverUnitState:
    unit: str
    status: str
    deployment_revision: str = ""

    def __post_init__(self) -> None:
        if self.unit not in CUTOVER_UNITS or self.status not in _CUTOVER_STATES:
            raise ValueError("cutover unit state is invalid")
        if self.status != "pending" and not _ID_RE.fullmatch(self.deployment_revision):
            raise ValueError("cutover deployment revision is invalid")


@dataclass(frozen=True, slots=True)
class CutoverAuditEvent:
    event_type: str
    unit: str
    deployment_revision: str
    detail_digest: str = ""


def _digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _revision(value: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise TrafficPolicyError("deployment revision is invalid")
    return value


def _legacy_decision(capability: str, routing_key: str, revision: str, reason: str) -> TrafficDecision:
    return TrafficDecision(
        capability=capability,
        deployment_revision=revision,
        route="legacy",
        reason=reason,
        key_digest=_digest(f"{capability}|{routing_key}"),
        traffic_percent=0,
        rollback_window_open=False,
    )


class CutoverStateStoreProtocol(Protocol):
    """Small structural protocol kept import-light for the continuity package."""

    def load(self, unit: str) -> CutoverUnitState | None:  # pragma: no cover - interface declaration
        raise NotImplementedError

    def save(self, state: CutoverUnitState) -> None:  # pragma: no cover - interface declaration
        raise NotImplementedError

    def append_event(self, event: CutoverAuditEvent) -> None:  # pragma: no cover - interface declaration
        raise NotImplementedError


class CapabilityCutoverCoordinator:
    """Enforce ordered unit promotion around the traffic controller."""

    def __init__(self, *, traffic: CapabilityTrafficController, store: CutoverStateStoreProtocol | None = None) -> None:
        self._traffic = traffic
        self._store = store
        self._states = {unit: CutoverUnitState(unit=unit, status="pending") for unit in CUTOVER_UNITS}
        self.events: list[CutoverAuditEvent] = []
        for unit in CUTOVER_UNITS:
            state = self._load(unit)
            if state is not None:
                self._states[unit] = state

    def _load(self, unit: str) -> CutoverUnitState | None:
        if self._store is not None:
            return self._store.load(unit)
        return self._states.get(unit)

    def _save(self, state: CutoverUnitState) -> None:
        self._states[state.unit] = state
        if self._store is not None:
            self._store.save(state)

    def _event(self, event: CutoverAuditEvent) -> None:
        self.events.append(event)
        if self._store is not None:
            self._store.append_event(event)

    def state(self, unit: str) -> CutoverUnitState:
        if unit not in CUTOVER_UNITS:
            raise ValueError("unsupported cutover unit")
        return self._states[unit]

    def promote(
        self,
        *,
        unit: str,
        deployment_revision: str,
        traffic_percent: int,
        rollback_window: timedelta,
    ) -> CutoverUnitState:
        if unit not in CUTOVER_UNITS:
            raise ValueError("unsupported cutover unit")
        deployment_revision = _revision(deployment_revision)
        current = self.state(unit)
        if current.status in {"active", "stable"}:
            if current.deployment_revision != deployment_revision:
                raise TrafficPolicyError("cutover unit cannot change revision while active")
            return current
        index = CUTOVER_UNITS.index(unit)
        if any(self.state(previous).status != "stable" for previous in CUTOVER_UNITS[:index]):
            raise TrafficPolicyError("previous cutover unit must be stable")
        # Configure every Capability before publishing the unit state.  If the
        # durable state write fails, the state remains non-routable and the
        # coordinator therefore fails closed even if a policy was persisted.
        for capability in CUTOVER_UNIT_CAPABILITIES[unit]:
            self._traffic.configure(
                capability=capability,
                deployment_revision=deployment_revision,
                enabled=True,
                traffic_percent=traffic_percent,
                rollback_window=rollback_window,
            )
        next_state = CutoverUnitState(unit=unit, status="active", deployment_revision=deployment_revision)
        self._save(next_state)
        self._event(CutoverAuditEvent("cutover_unit_promoted", unit, deployment_revision))
        return next_state

    def stabilize(self, *, unit: str, health_proof: str) -> CutoverUnitState:
        current = self.state(unit)
        if current.status != "active":
            raise TrafficPolicyError("only an active cutover unit can become stable")
        if not isinstance(health_proof, str) or not health_proof.strip() or len(health_proof) > 160:
            raise TrafficPolicyError("health proof is invalid")
        next_state = CutoverUnitState(unit=unit, status="stable", deployment_revision=current.deployment_revision)
        self._save(next_state)
        self._event(CutoverAuditEvent("cutover_unit_stabilized", unit, current.deployment_revision, _digest(health_proof)))
        return next_state

    def rollback(self, *, unit: str, reason: str) -> None:
        if unit not in CUTOVER_UNITS:
            raise ValueError("unsupported cutover unit")
        if not isinstance(reason, str) or not reason.strip():
            raise TrafficPolicyError("rollback reason must not be empty")
        start = CUTOVER_UNITS.index(unit)
        for affected in CUTOVER_UNITS[start:]:
            current = self.state(affected)
            if current.status == "pending":
                continue
            self._save(
                CutoverUnitState(
                    unit=affected,
                    status="rolled_back",
                    deployment_revision=current.deployment_revision,
                )
            )
            for capability in CUTOVER_UNIT_CAPABILITIES[affected]:
                try:
                    self._traffic.record_failure(
                        capability=capability,
                        deployment_revision=current.deployment_revision,
                        detail="cutover rollback",
                    )
                except TrafficPolicyError:
                    # The state gate is already legacy-only; a missing policy
                    # must not turn rollback into a fail-open route.
                    pass
        self._event(
            CutoverAuditEvent(
                "cutover_rolled_back",
                unit,
                self.state(unit).deployment_revision,
                _digest(reason),
            )
        )

    def dispatch(
        self,
        *,
        capability: str,
        routing_key: str,
        deployment_revision: str,
        request: Any,
        platform_handler: Callable[[Any], Any],
        legacy_handler: Callable[[Any], Any],
    ) -> TrafficDispatchResult:
        unit = next((name for name, capabilities in CUTOVER_UNIT_CAPABILITIES.items() if capability in capabilities), None)
        if unit is None:
            raise TrafficPolicyError("capability is not part of the cutover plan")
        state = self.state(unit)
        if state.status not in {"active", "stable"}:
            return TrafficDispatchResult(
                result=legacy_handler(request),
                decision=_legacy_decision(capability, routing_key, deployment_revision, "unit_not_promoted"),
            )
        result = CapabilityTrafficDispatcher(self._traffic).dispatch(
            capability=capability,
            routing_key=routing_key,
            deployment_revision=deployment_revision,
            request=request,
            platform_handler=platform_handler,
            legacy_handler=legacy_handler,
        )
        if result.fallback:
            self.rollback(unit=unit, reason=f"{capability} failure")
        return result


__all__ = [
    "CUTOVER_UNIT_CAPABILITIES",
    "CUTOVER_UNITS",
    "CapabilityCutoverCoordinator",
    "CutoverAuditEvent",
    "CutoverStateStoreProtocol",
    "CutoverUnitState",
]
