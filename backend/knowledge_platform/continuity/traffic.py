"""Fail-closed capability traffic decisions for local sidecar rehearsals.

The controller is deliberately framework-neutral.  It models the decision
that a production supervisor would make, but it does not own a server, auth,
deployment, or legacy worker process.  Raw routing keys and failure details
never enter the audit events.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, TypeVar

from .ports import CONTINUITY_CAPABILITIES

_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,160}$")
_MAX_ROLLBACK_WINDOW = timedelta(days=7)

QUERY_TRAFFIC_CAPABILITIES: tuple[str, ...] = (
    "knowledge_list",
    "knowledge_search",
    "knowledge_read",
    "document_rag_query",
    "wiki_query",
    "table_query",
    # NL2SQL planning and readonly execution are one atomic cutover unit.
    "database_nl2sql_execute_readonly",
)
TRAFFIC_CAPABILITIES: tuple[str, ...] = QUERY_TRAFFIC_CAPABILITIES + CONTINUITY_CAPABILITIES


class TrafficPolicyError(ValueError):
    """A traffic policy or route decision cannot safely advance."""


@dataclass(frozen=True, slots=True)
class TrafficDecision:
    capability: str
    deployment_revision: str
    route: str
    reason: str
    key_digest: str
    traffic_percent: int
    rollback_window_open: bool


@dataclass(frozen=True, slots=True)
class TrafficAuditEvent:
    event_type: str
    capability: str
    deployment_revision: str
    reason: str
    key_digest: str = ""
    detail_digest: str = ""


@dataclass(frozen=True, slots=True)
class TrafficPolicySnapshot:
    capability: str
    deployment_revision: str
    enabled: bool
    traffic_percent: int
    healthy: bool
    rollback_deadline: datetime

    def __post_init__(self) -> None:
        _validate_id("capability", self.capability)
        _validate_id("deployment revision", self.deployment_revision)
        if not isinstance(self.enabled, bool) or not isinstance(self.healthy, bool):
            raise TrafficPolicyError("traffic flags must be boolean")
        if isinstance(self.traffic_percent, bool) or not isinstance(self.traffic_percent, int) or not 0 <= self.traffic_percent <= 100:
            raise TrafficPolicyError("traffic percent must be an integer from 0 to 100")
        if (
            not isinstance(self.rollback_deadline, datetime)
            or self.rollback_deadline.tzinfo is None
            or self.rollback_deadline.utcoffset() is None
        ):
            raise TrafficPolicyError("rollback deadline must be timezone-aware")


class TrafficPolicyStore(Protocol):
    def load(self, capability: str) -> TrafficPolicySnapshot | None: ...

    def save(self, policy: TrafficPolicySnapshot) -> None: ...

    def append_event(self, event: TrafficAuditEvent) -> None: ...


ResultT = TypeVar("ResultT")


@dataclass(frozen=True, slots=True)
class TrafficDispatchResult:
    result: Any
    decision: TrafficDecision
    fallback: bool = False


@dataclass(slots=True)
class _Policy:
    deployment_revision: str
    enabled: bool
    traffic_percent: int
    healthy: bool
    rollback_deadline: datetime


def _digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _validate_id(name: str, value: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise TrafficPolicyError(f"invalid {name}")
    return value


class CapabilityTrafficController:
    """Choose Platform or legacy per Capability under an explicit policy."""

    def __init__(
        self,
        *,
        capabilities: Iterable[str] = TRAFFIC_CAPABILITIES,
        clock: Callable[[], datetime] | None = None,
        store: TrafficPolicyStore | None = None,
        active_revision_check: Callable[[str], bool] | None = None,
    ) -> None:
        supported = tuple(capabilities)
        if not supported or len(set(supported)) != len(supported):
            raise TrafficPolicyError("traffic capabilities must be non-empty and unique")
        if any(capability not in TRAFFIC_CAPABILITIES for capability in supported):
            raise TrafficPolicyError("traffic capabilities contain an unsupported value")
        self._capabilities = frozenset(supported)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._store = store
        if active_revision_check is not None and not callable(active_revision_check):
            raise TrafficPolicyError("active revision check must be callable")
        self._active_revision_check = active_revision_check
        self._policies: dict[str, _Policy] = {}
        self.events: list[TrafficAuditEvent] = []

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise TrafficPolicyError("traffic clock must return an aware datetime")
        return value.astimezone(UTC)

    def _require_capability(self, capability: str) -> str:
        if capability not in self._capabilities:
            raise TrafficPolicyError("unsupported traffic capability")
        return capability

    def _load_policy(self, capability: str) -> _Policy | None:
        if self._store is not None:
            snapshot = self._store.load(capability)
            if snapshot is None:
                return None
            return _Policy(
                deployment_revision=snapshot.deployment_revision,
                enabled=snapshot.enabled,
                traffic_percent=snapshot.traffic_percent,
                healthy=snapshot.healthy,
                rollback_deadline=snapshot.rollback_deadline,
            )
        return self._policies.get(capability)

    def _save_policy(self, capability: str, policy: _Policy) -> None:
        self._policies[capability] = policy
        if self._store is not None:
            self._store.save(
                TrafficPolicySnapshot(
                    capability=capability,
                    deployment_revision=policy.deployment_revision,
                    enabled=policy.enabled,
                    traffic_percent=policy.traffic_percent,
                    healthy=policy.healthy,
                    rollback_deadline=policy.rollback_deadline,
                )
            )

    def _append_event(self, event: TrafficAuditEvent) -> None:
        self.events.append(event)
        if self._store is not None:
            self._store.append_event(event)

    def configure(
        self,
        *,
        capability: str,
        deployment_revision: str,
        enabled: bool,
        traffic_percent: int,
        rollback_window: timedelta,
        healthy: bool = True,
    ) -> None:
        capability = self._require_capability(capability)
        deployment_revision = _validate_id("deployment revision", deployment_revision)
        if not isinstance(enabled, bool) or not isinstance(healthy, bool):
            raise TrafficPolicyError("traffic flags must be boolean")
        if isinstance(traffic_percent, bool) or not isinstance(traffic_percent, int) or not 0 <= traffic_percent <= 100:
            raise TrafficPolicyError("traffic percent must be an integer from 0 to 100")
        if not isinstance(rollback_window, timedelta) or rollback_window <= timedelta(0) or rollback_window > _MAX_ROLLBACK_WINDOW:
            raise TrafficPolicyError("rollback window is outside the allowed range")
        now = self._now()
        prior = self._load_policy(capability)
        if prior is not None and prior.deployment_revision != deployment_revision:
            raise TrafficPolicyError("a capability cannot change deployment revision while configured")
        self._save_policy(
            capability,
            _Policy(
                deployment_revision=deployment_revision,
                enabled=enabled,
                traffic_percent=traffic_percent,
                healthy=healthy,
                rollback_deadline=now + rollback_window,
            ),
        )
        self._append_event(
            TrafficAuditEvent(
                event_type="traffic_policy_configured",
                capability=capability,
                deployment_revision=deployment_revision,
                reason="enabled" if enabled else "disabled",
            )
        )

    def set_health(self, *, capability: str, deployment_revision: str, healthy: bool, detail: str = "") -> None:
        capability = self._require_capability(capability)
        deployment_revision = _validate_id("deployment revision", deployment_revision)
        if not isinstance(healthy, bool):
            raise TrafficPolicyError("health must be boolean")
        if not isinstance(detail, str):
            raise TrafficPolicyError("health detail must be a string")
        policy = self._load_policy(capability)
        if policy is None or policy.deployment_revision != deployment_revision:
            raise TrafficPolicyError("traffic policy is not configured for this revision")
        policy.healthy = healthy
        if not healthy:
            policy.enabled = False
        self._save_policy(capability, policy)
        self._append_event(
            TrafficAuditEvent(
                event_type="traffic_health_updated",
                capability=capability,
                deployment_revision=deployment_revision,
                reason="healthy" if healthy else "unhealthy",
                detail_digest=_digest(detail) if detail else "",
            )
        )

    def record_failure(self, *, capability: str, deployment_revision: str, detail: str) -> None:
        capability = self._require_capability(capability)
        deployment_revision = _validate_id("deployment revision", deployment_revision)
        if not isinstance(detail, str) or not detail.strip():
            raise TrafficPolicyError("failure detail must not be empty")
        policy = self._load_policy(capability)
        if policy is None or policy.deployment_revision != deployment_revision:
            raise TrafficPolicyError("traffic policy is not configured for this revision")
        policy.enabled = False
        policy.healthy = False
        self._save_policy(capability, policy)
        self._append_event(
            TrafficAuditEvent(
                event_type="traffic_cut_back_to_legacy",
                capability=capability,
                deployment_revision=deployment_revision,
                reason="capability_failure",
                detail_digest=_digest(detail),
            )
        )

    def decide(self, *, capability: str, routing_key: str, deployment_revision: str) -> TrafficDecision:
        capability = self._require_capability(capability)
        deployment_revision = _validate_id("deployment revision", deployment_revision)
        if not isinstance(routing_key, str) or not routing_key.strip() or len(routing_key) > 160:
            raise TrafficPolicyError("routing key is invalid")
        key_digest = _digest(f"{capability}|{routing_key}")
        policy = self._load_policy(capability)
        if policy is None:
            return self._decision(capability, deployment_revision, "legacy", "not_configured", key_digest, 0, False)
        if policy.deployment_revision != deployment_revision:
            raise TrafficPolicyError("request revision does not match traffic policy")
        if self._active_revision_check is not None:
            try:
                active = self._active_revision_check(deployment_revision)
            except Exception:
                active = False
            if active is not True:
                return self._decision(
                    capability,
                    deployment_revision,
                    "legacy",
                    "deployment_not_active",
                    key_digest,
                    policy.traffic_percent,
                    False,
                )
        if not policy.enabled:
            reason = "unhealthy" if not policy.healthy else "feature_disabled"
            return self._decision(capability, deployment_revision, "legacy", reason, key_digest, policy.traffic_percent, False)
        if not policy.healthy:
            return self._decision(capability, deployment_revision, "legacy", "unhealthy", key_digest, policy.traffic_percent, False)
        if self._now() >= policy.rollback_deadline:
            return self._decision(
                capability,
                deployment_revision,
                "legacy",
                "rollback_window_expired",
                key_digest,
                policy.traffic_percent,
                False,
            )
        bucket = int(hashlib.sha256(f"{capability}|{routing_key}".encode()).hexdigest()[:8], 16) % 100
        route = "platform" if bucket < policy.traffic_percent else "legacy"
        return self._decision(capability, deployment_revision, route, "within_window", key_digest, policy.traffic_percent, True)

    @staticmethod
    def _decision(
        capability: str,
        deployment_revision: str,
        route: str,
        reason: str,
        key_digest: str,
        traffic_percent: int,
        rollback_window_open: bool,
    ) -> TrafficDecision:
        return TrafficDecision(
            capability=capability,
            deployment_revision=deployment_revision,
            route=route,
            reason=reason,
            key_digest=key_digest,
            traffic_percent=traffic_percent,
            rollback_window_open=rollback_window_open,
        )


class CapabilityTrafficDispatcher:
    """Execute one Platform/legacy handler under a traffic decision."""

    def __init__(self, controller: CapabilityTrafficController) -> None:
        self._controller = controller

    def dispatch(
        self,
        *,
        capability: str,
        routing_key: str,
        deployment_revision: str,
        request: ResultT,
        platform_handler: Callable[[ResultT], Any],
        legacy_handler: Callable[[ResultT], Any],
    ) -> TrafficDispatchResult:
        decision = self._controller.decide(
            capability=capability,
            routing_key=routing_key,
            deployment_revision=deployment_revision,
        )
        if decision.route == "legacy":
            return TrafficDispatchResult(result=legacy_handler(request), decision=decision)
        try:
            return TrafficDispatchResult(result=platform_handler(request), decision=decision)
        except Exception as error:
            self._controller.record_failure(
                capability=capability,
                deployment_revision=deployment_revision,
                detail=type(error).__name__,
            )
            fallback_decision = self._controller.decide(
                capability=capability,
                routing_key=routing_key,
                deployment_revision=deployment_revision,
            )
            return TrafficDispatchResult(
                result=legacy_handler(request),
                decision=fallback_decision,
                fallback=True,
            )


__all__ = [
    "CapabilityTrafficController",
    "CapabilityTrafficDispatcher",
    "QUERY_TRAFFIC_CAPABILITIES",
    "TRAFFIC_CAPABILITIES",
    "TrafficAuditEvent",
    "TrafficDecision",
    "TrafficDispatchResult",
    "TrafficPolicyError",
    "TrafficPolicySnapshot",
    "TrafficPolicyStore",
]
