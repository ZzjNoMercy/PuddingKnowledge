"""In-memory local implementation of the Platform continuity boundary."""

from __future__ import annotations

import hashlib
import re

from .ports import (
    CONTINUITY_CAPABILITIES,
    CapabilityHandlers,
    ContinuityAuditEvent,
    ContinuityRequest,
    ContinuityResult,
    LegacyWorkerControl,
)

_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,160}$")


class ContinuityError(RuntimeError):
    """A continuity drill cannot safely advance or submit a result."""


class LocalLegacyWorkerControl:
    """Explicit stop/start switch used by the local rollback rehearsal."""

    def __init__(self) -> None:
        self._enabled = True
        self.events: list[str] = []

    @property
    def enabled(self) -> bool:
        return self._enabled

    def stop(self) -> None:
        if not self._enabled:
            return
        self._enabled = False
        self.events.append("stopped")

    def start(self) -> None:
        if self._enabled:
            return
        self._enabled = True
        self.events.append("started")


class LocalPlatformSidecar:
    """Run one fixed-revision Platform slice while legacy is stopped.

    This is intentionally a finite local supervisor rather than a background
    daemon.  A real sidecar can reuse the same boundary, but deployment,
    scheduling, and process supervision remain outside this rehearsal.
    """

    def __init__(self, *, legacy: LegacyWorkerControl, handlers: CapabilityHandlers) -> None:
        unknown = set(handlers) - set(CONTINUITY_CAPABILITIES)
        if unknown:
            raise ValueError("handlers contain unsupported capabilities")
        self._legacy = legacy
        self._handlers = dict(handlers)
        self._active = False
        self._active_revision = ""
        self._rollback_revision = "legacy"
        self._results: dict[str, ContinuityResult] = {}
        self.events: list[ContinuityAuditEvent] = []

    @property
    def active_revision(self) -> str:
        return self._active_revision

    @property
    def active(self) -> bool:
        return self._active

    def activate(self, *, deployment_revision: str) -> None:
        if not _ID_RE.fullmatch(deployment_revision):
            raise ValueError("invalid deployment revision")
        if self._legacy.enabled:
            raise ContinuityError("legacy workers must be stopped before sidecar activation")
        if self._active:
            if self._active_revision != deployment_revision:
                raise ContinuityError("a sidecar cannot switch revisions while active")
            return
        self._active_revision = deployment_revision
        self._active = True
        self.events.append(
            ContinuityAuditEvent(
                event_type="sidecar_activated",
                capability="continuity",
                space_id="system",
                deployment_revision=deployment_revision,
            )
        )

    def process(self, request: ContinuityRequest) -> ContinuityResult:
        if not self._active:
            raise ContinuityError("Platform sidecar is not active")
        if request.deployment_revision != self._active_revision:
            raise ContinuityError("request revision does not match active revision")
        handler = self._handlers.get(request.capability)
        if handler is None:
            raise ContinuityError("capability is not enabled in this sidecar")
        key_digest = hashlib.sha256(
            "|".join(
                (request.capability, request.space_id, request.resource_key, request.idempotency_key)
            ).encode("utf-8")
        ).hexdigest()
        prior = self._results.get(key_digest)
        if prior is not None:
            return ContinuityResult(
                capability=prior.capability,
                space_id=prior.space_id,
                resource_uri=prior.resource_uri,
                deployment_revision=prior.deployment_revision,
                replayed=True,
            )
        try:
            resource_uri = handler(request)
            result = ContinuityResult(
                capability=request.capability,
                space_id=request.space_id,
                resource_uri=resource_uri,
                deployment_revision=self._active_revision,
            )
        except Exception as exc:
            self.events.append(
                ContinuityAuditEvent(
                    event_type="capability_failed",
                    capability=request.capability,
                    space_id=request.space_id,
                    deployment_revision=self._active_revision,
                    idempotency_digest=key_digest,
                    detail_digest=hashlib.sha256(type(exc).__name__.encode("utf-8")).hexdigest(),
                )
            )
            raise
        self._results[key_digest] = result
        self.events.append(
            ContinuityAuditEvent(
                event_type="capability_completed",
                capability=request.capability,
                space_id=request.space_id,
                deployment_revision=self._active_revision,
                idempotency_digest=key_digest,
            )
        )
        return result

    def rollback(self, *, reason: str) -> None:
        if not reason.strip():
            raise ValueError("rollback reason must not be empty")
        if not self._active:
            self._legacy.start()
            return
        prior_revision = self._active_revision
        self._active = False
        self._active_revision = ""
        self._legacy.start()
        self.events.append(
            ContinuityAuditEvent(
                event_type="rolled_back_to_legacy",
                capability="continuity",
                space_id="system",
                deployment_revision=self._rollback_revision,
                detail_digest=hashlib.sha256(reason.encode("utf-8")).hexdigest(),
            )
        )
        self.events.append(
            ContinuityAuditEvent(
                event_type="sidecar_revision_closed",
                capability="continuity",
                space_id="system",
                deployment_revision=prior_revision,
            )
        )
