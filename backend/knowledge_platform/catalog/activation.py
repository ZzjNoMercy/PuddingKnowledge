"""Fail-closed Catalog activation state for local cutover rehearsals."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_STATES = frozenset({"pending", "prepared", "drained", "verified", "activated", "rolled_back"})
_REQUIRED_CHECKS = frozenset({"source_unchanged", "target_digest_matches", "catalog_integrity"})


class CatalogActivationError(RuntimeError):
    """A Catalog activation transition cannot safely advance."""


@dataclass(frozen=True, slots=True)
class CatalogActivationState:
    installation_id: str
    source_revision: str
    target_revision: str
    source_digest: str
    target_digest: str
    active_revision: str
    status: str
    source_read_only: bool
    target_read_only: bool

    def __post_init__(self) -> None:
        if self.status not in _STATES:
            raise ValueError("Catalog activation state is invalid")
        if not isinstance(self.source_read_only, bool) or not isinstance(self.target_read_only, bool):
            raise ValueError("Catalog activation read-only flags must be boolean")
        if self.status == "pending":
            if any((
                self.installation_id,
                self.source_revision,
                self.target_revision,
                self.source_digest,
                self.target_digest,
                self.active_revision,
            )):
                raise ValueError("pending Catalog activation state must not carry revisions")
            if self.source_read_only or self.target_read_only:
                raise ValueError("pending Catalog activation state must leave both copies writable")
            return
        for name, value in (
            ("installation id", self.installation_id),
            ("source revision", self.source_revision),
            ("target revision", self.target_revision),
        ):
            if not isinstance(value, str) or not _ID_RE.fullmatch(value):
                raise ValueError(f"invalid Catalog activation {name}")
        if self.source_revision == self.target_revision:
            raise ValueError("Catalog activation source and target revisions must differ")
        _require_digest(self.source_digest, "source digest")
        _require_digest(self.target_digest, "target digest")
        if not isinstance(self.active_revision, str) or not _ID_RE.fullmatch(self.active_revision):
            raise ValueError("invalid Catalog activation active revision")
        expected_flags = {
            "prepared": (False, True),
            "drained": (False, True),
            "verified": (False, True),
            "activated": (True, False),
            "rolled_back": (False, True),
        }[self.status]
        if (self.source_read_only, self.target_read_only) != expected_flags:
            raise ValueError("Catalog activation read-only flags do not match state")


@dataclass(frozen=True, slots=True)
class CatalogActivationAuditEvent:
    event_type: str
    installation_id: str
    source_revision: str
    target_revision: str
    active_revision: str
    detail_digest: str = ""


class CatalogActivationStore(Protocol):
    def load(self) -> CatalogActivationState | None: ...

    def save(self, state: CatalogActivationState) -> None: ...

    def append_event(self, event: CatalogActivationAuditEvent) -> None: ...


def _digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _require_digest(value: str, label: str) -> str:
    if not isinstance(value, str) or not _DIGEST_RE.fullmatch(value):
        raise CatalogActivationError(f"{label} must be a sha256 digest")
    return value


class CatalogActivationController:
    """Coordinate local activation without modifying either Catalog file."""

    def __init__(self, *, store: CatalogActivationStore) -> None:
        self._store = store
        self._state = store.load() or CatalogActivationState(
            installation_id="",
            source_revision="",
            target_revision="",
            source_digest="",
            target_digest="",
            active_revision="",
            status="pending",
            source_read_only=False,
            target_read_only=False,
        )
        self.events: list[CatalogActivationAuditEvent] = []

    @property
    def state(self) -> CatalogActivationState:
        return self._state

    def _save(self, state: CatalogActivationState) -> None:
        self._store.save(state)
        self._state = state

    def _event(self, event: CatalogActivationAuditEvent) -> None:
        self._store.append_event(event)
        self.events.append(event)

    def prepare(
        self,
        *,
        installation_id: str,
        source_revision: str,
        target_revision: str,
        source_digest: str,
        target_digest: str,
    ) -> CatalogActivationState:
        if self._state.status not in {"pending", "rolled_back"}:
            raise CatalogActivationError("Catalog activation is already in progress or complete")
        if not all(
            isinstance(value, str) and _ID_RE.fullmatch(value)
            for value in (installation_id, source_revision, target_revision)
        ):
            raise CatalogActivationError("Catalog activation identity is invalid")
        if source_revision == target_revision:
            raise CatalogActivationError("Catalog activation source and target revisions must differ")
        _require_digest(source_digest, "source digest")
        _require_digest(target_digest, "target digest")
        state = CatalogActivationState(
            installation_id=installation_id,
            source_revision=source_revision,
            target_revision=target_revision,
            source_digest=source_digest,
            target_digest=target_digest,
            active_revision=source_revision,
            status="prepared",
            source_read_only=False,
            target_read_only=True,
        )
        self._save(state)
        self._event(CatalogActivationAuditEvent("activation_prepared", installation_id, source_revision, target_revision, source_revision))
        return state

    def mark_drained(self, *, proof_digest: str) -> CatalogActivationState:
        if self._state.status != "prepared":
            raise CatalogActivationError("Catalog must be prepared before drain proof")
        proof_digest = _require_digest(proof_digest, "drain proof")
        state = CatalogActivationState(
            installation_id=self._state.installation_id,
            source_revision=self._state.source_revision,
            target_revision=self._state.target_revision,
            source_digest=self._state.source_digest,
            target_digest=self._state.target_digest,
            active_revision=self._state.source_revision,
            status="drained",
            source_read_only=False,
            target_read_only=True,
        )
        self._save(state)
        self._event(CatalogActivationAuditEvent("activation_drained", state.installation_id, state.source_revision, state.target_revision, state.active_revision, proof_digest))
        return state

    def verify(
        self,
        *,
        source_before_digest: str,
        source_after_digest: str,
        target_digest: str,
        expected_target_digest: str,
        checks: Mapping[str, bool],
    ) -> CatalogActivationState:
        if self._state.status != "drained":
            raise CatalogActivationError("Catalog must be drained before verification")
        for value, label in (
            (source_before_digest, "source before digest"),
            (source_after_digest, "source after digest"),
            (target_digest, "target digest"),
            (expected_target_digest, "expected target digest"),
        ):
            _require_digest(value, label)
        if set(checks) != _REQUIRED_CHECKS or not all(type(value) is bool for value in checks.values()):
            raise CatalogActivationError("Catalog verification checks are incomplete")
        if source_before_digest != source_after_digest or target_digest != expected_target_digest or not all(checks.values()):
            raise CatalogActivationError("Catalog activation verification failed")
        state = CatalogActivationState(
            installation_id=self._state.installation_id,
            source_revision=self._state.source_revision,
            target_revision=self._state.target_revision,
            source_digest=source_before_digest,
            target_digest=target_digest,
            active_revision=self._state.source_revision,
            status="verified",
            source_read_only=False,
            target_read_only=True,
        )
        self._save(state)
        self._event(CatalogActivationAuditEvent("activation_verified", state.installation_id, state.source_revision, state.target_revision, state.active_revision, _digest(target_digest)))
        return state

    def activate(self, *, deployment_revision: str) -> CatalogActivationState:
        if self._state.status != "verified":
            raise CatalogActivationError("catalog activation requires verification before activation")
        if deployment_revision != self._state.target_revision:
            raise CatalogActivationError("activation revision does not match verified target")
        state = CatalogActivationState(
            installation_id=self._state.installation_id,
            source_revision=self._state.source_revision,
            target_revision=self._state.target_revision,
            source_digest=self._state.source_digest,
            target_digest=self._state.target_digest,
            active_revision=self._state.target_revision,
            status="activated",
            source_read_only=True,
            target_read_only=False,
        )
        self._save(state)
        self._event(CatalogActivationAuditEvent("activation_committed", state.installation_id, state.source_revision, state.target_revision, state.active_revision))
        return state

    def rollback(self, *, reason: str) -> CatalogActivationState:
        if self._state.status not in {"prepared", "drained", "verified", "activated"}:
            raise CatalogActivationError("Catalog activation has no rollbackable state")
        if not isinstance(reason, str) or not reason.strip():
            raise CatalogActivationError("Catalog rollback reason must not be empty")
        state = CatalogActivationState(
            installation_id=self._state.installation_id,
            source_revision=self._state.source_revision,
            target_revision=self._state.target_revision,
            source_digest=self._state.source_digest,
            target_digest=self._state.target_digest,
            active_revision=self._state.source_revision,
            status="rolled_back",
            source_read_only=False,
            target_read_only=True,
        )
        self._save(state)
        self._event(CatalogActivationAuditEvent("activation_rolled_back", state.installation_id, state.source_revision, state.target_revision, state.active_revision, _digest(reason)))
        return state


__all__ = [
    "CatalogActivationAuditEvent",
    "CatalogActivationController",
    "CatalogActivationError",
    "CatalogActivationState",
    "CatalogActivationStore",
]
