"""Fail-closed deployment revision bundles for local cutover rehearsals."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ARTIFACT_KINDS = frozenset({"catalog", "blob", "vector_index", "wiki_root"})
_STATES = frozenset({"pending", "prepared", "drained", "verified", "activated", "rolled_back"})
_REQUIRED_CHECKS = frozenset({"legacy_unchanged", "candidate_manifest_matches", "bundle_integrity"})


class DeploymentActivationError(RuntimeError):
    """A deployment revision cannot safely advance or be read."""


def _require_id(value: str, label: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise DeploymentActivationError(f"{label} is invalid")
    return value


def _require_digest(value: str, label: str) -> str:
    if not isinstance(value, str) or not _DIGEST_RE.fullmatch(value):
        raise DeploymentActivationError(f"{label} must be a sha256 digest")
    return value


@dataclass(frozen=True, slots=True)
class DeploymentArtifact:
    """A portable artifact handle; physical paths and secrets are out of contract."""

    kind: str
    revision: str
    resource_id: str
    locator_digest: str

    def __post_init__(self) -> None:
        if self.kind not in _ARTIFACT_KINDS:
            raise ValueError("deployment artifact kind is invalid")
        _require_id(self.revision, "deployment artifact revision")
        _require_id(self.resource_id, "deployment artifact resource id")
        if "/" in self.resource_id or "\\" in self.resource_id or ".." in self.resource_id:
            raise ValueError("deployment artifact resource id must be portable")
        _require_digest(self.locator_digest, "deployment artifact locator digest")

    def to_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "revision": self.revision,
            "resource_id": self.resource_id,
            "locator_digest": self.locator_digest,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> DeploymentArtifact:
        if set(value) != {"kind", "revision", "resource_id", "locator_digest"}:
            raise ValueError("deployment artifact fields are invalid")
        return cls(
            kind=value["kind"],
            revision=value["revision"],
            resource_id=value["resource_id"],
            locator_digest=value["locator_digest"],
        )


@dataclass(frozen=True, slots=True)
class DeploymentManifest:
    """The complete set of resources selected by one deployment revision."""

    deployment_revision: str
    artifacts: tuple[DeploymentArtifact, ...]

    def __post_init__(self) -> None:
        _require_id(self.deployment_revision, "deployment revision")
        if not isinstance(self.artifacts, tuple) or len(self.artifacts) != len(_ARTIFACT_KINDS):
            raise ValueError("deployment manifest must contain exactly four artifacts")
        kinds = {artifact.kind for artifact in self.artifacts}
        if kinds != _ARTIFACT_KINDS:
            raise ValueError("deployment manifest must contain one artifact of each required kind")
        if any(artifact.revision != self.deployment_revision for artifact in self.artifacts):
            raise ValueError("deployment artifact revisions must match deployment revision")

    def to_dict(self) -> dict[str, object]:
        return {
            "deployment_revision": self.deployment_revision,
            "artifacts": [artifact.to_dict() for artifact in sorted(self.artifacts, key=lambda item: item.kind)],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> DeploymentManifest:
        if set(value) != {"deployment_revision", "artifacts"}:
            raise ValueError("deployment manifest fields are invalid")
        artifacts = value["artifacts"]
        if not isinstance(artifacts, list):
            raise ValueError("deployment manifest artifacts must be a list")
        return cls(
            deployment_revision=value["deployment_revision"],
            artifacts=tuple(DeploymentArtifact.from_dict(item) for item in artifacts),
        )

    def manifest_digest(self) -> str:
        encoded = json.dumps(self.to_dict(), ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class DeploymentActivationState:
    installation_id: str
    legacy_manifest: DeploymentManifest | None
    candidate_manifest: DeploymentManifest | None
    active_deployment_revision: str
    status: str
    legacy_read_only: bool
    candidate_read_only: bool

    def __post_init__(self) -> None:
        if self.status not in _STATES:
            raise ValueError("deployment activation state is invalid")
        if not isinstance(self.legacy_read_only, bool) or not isinstance(self.candidate_read_only, bool):
            raise ValueError("deployment activation read-only flags must be boolean")
        if self.status == "pending":
            if (
                self.installation_id
                or self.legacy_manifest is not None
                or self.candidate_manifest is not None
                or self.active_deployment_revision
            ):
                raise ValueError("pending deployment state must not carry a bundle")
            if self.legacy_read_only or self.candidate_read_only:
                raise ValueError("pending deployment state must leave both sides writable")
            return
        if self.legacy_manifest is None or self.candidate_manifest is None:
            raise ValueError("non-pending deployment state requires both bundles")
        _require_id(self.installation_id, "installation id")
        if self.legacy_manifest.deployment_revision == self.candidate_manifest.deployment_revision:
            raise ValueError("legacy and candidate deployment revisions must differ")
        if self.active_deployment_revision not in {
            self.legacy_manifest.deployment_revision,
            self.candidate_manifest.deployment_revision,
        }:
            raise ValueError("active deployment revision is not part of the bundle")
        expected_flags = {
            "prepared": (False, True),
            "drained": (False, True),
            "verified": (False, True),
            "activated": (True, False),
            "rolled_back": (False, True),
        }[self.status]
        if (self.legacy_read_only, self.candidate_read_only) != expected_flags:
            raise ValueError("deployment activation read-only flags do not match state")


@dataclass(frozen=True, slots=True)
class DeploymentActivationAuditEvent:
    event_type: str
    active_deployment_revision: str
    detail_digest: str = ""


@dataclass(frozen=True, slots=True)
class DeploymentReadContext:
    """Opaque snapshot token for one request's complete deployment bundle."""

    installation_id: str
    deployment_revision: str
    manifest_digest: str


class DeploymentActivationStore(Protocol):
    def load(self) -> DeploymentActivationState | None: ...

    def save(self, state: DeploymentActivationState) -> None: ...

    def append_event(self, event: DeploymentActivationAuditEvent) -> None: ...


def _digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


class DeploymentActivationController:
    """Atomically select one complete, verified deployment bundle."""

    def __init__(self, *, store: DeploymentActivationStore) -> None:
        self._store = store
        self._state = store.load() or DeploymentActivationState("", None, None, "", "pending", False, False)
        self.events: list[DeploymentActivationAuditEvent] = []

    @property
    def state(self) -> DeploymentActivationState:
        return self._state

    def _save(self, state: DeploymentActivationState) -> None:
        self._store.save(state)
        self._state = state

    def _event(self, event: DeploymentActivationAuditEvent) -> None:
        self._store.append_event(event)
        self.events.append(event)

    def _refresh(self) -> None:
        persisted = self._store.load()
        if persisted is not None:
            self._state = persisted

    def prepare(
        self,
        *,
        installation_id: str,
        legacy_manifest: DeploymentManifest,
        candidate_manifest: DeploymentManifest,
    ) -> DeploymentActivationState:
        if self._state.status not in {"pending", "rolled_back"}:
            raise DeploymentActivationError("deployment activation is already in progress or complete")
        _require_id(installation_id, "installation id")
        if not isinstance(legacy_manifest, DeploymentManifest) or not isinstance(candidate_manifest, DeploymentManifest):
            raise DeploymentActivationError("deployment manifests are invalid")
        if legacy_manifest.deployment_revision == candidate_manifest.deployment_revision:
            raise DeploymentActivationError("legacy and candidate deployment revisions must differ")
        state = DeploymentActivationState(
            installation_id=installation_id,
            legacy_manifest=legacy_manifest,
            candidate_manifest=candidate_manifest,
            active_deployment_revision=legacy_manifest.deployment_revision,
            status="prepared",
            legacy_read_only=False,
            candidate_read_only=True,
        )
        self._save(state)
        self._event(DeploymentActivationAuditEvent("deployment_activation_prepared", state.active_deployment_revision))
        return state

    def mark_drained(self, *, proof_digest: str) -> DeploymentActivationState:
        if self._state.status != "prepared":
            raise DeploymentActivationError("deployment must be prepared before drain proof")
        proof_digest = _require_digest(proof_digest, "drain proof")
        state = DeploymentActivationState(
            installation_id=self._state.installation_id,
            legacy_manifest=self._state.legacy_manifest,
            candidate_manifest=self._state.candidate_manifest,
            active_deployment_revision=self._state.legacy_manifest.deployment_revision,
            status="drained",
            legacy_read_only=False,
            candidate_read_only=True,
        )
        self._save(state)
        self._event(DeploymentActivationAuditEvent("deployment_activation_drained", state.active_deployment_revision, proof_digest))
        return state

    def verify(
        self,
        *,
        legacy_before_digest: str,
        legacy_after_digest: str,
        candidate_manifest_digest: str,
        checks: Mapping[str, bool],
    ) -> DeploymentActivationState:
        if self._state.status != "drained":
            raise DeploymentActivationError("deployment must be drained before verification")
        legacy_before_digest = _require_digest(legacy_before_digest, "legacy before digest")
        legacy_after_digest = _require_digest(legacy_after_digest, "legacy after digest")
        candidate_manifest_digest = _require_digest(candidate_manifest_digest, "candidate manifest digest")
        if not isinstance(checks, Mapping) or set(checks) != _REQUIRED_CHECKS or not all(
            type(value) is bool for value in checks.values()
        ):
            raise DeploymentActivationError("deployment verification checks are incomplete")
        legacy_digest = self._state.legacy_manifest.manifest_digest()
        expected_candidate_digest = self._state.candidate_manifest.manifest_digest()
        if (
            legacy_before_digest != legacy_digest
            or legacy_after_digest != legacy_digest
            or candidate_manifest_digest != expected_candidate_digest
            or not all(checks.values())
        ):
            raise DeploymentActivationError("deployment activation verification failed")
        state = DeploymentActivationState(
            installation_id=self._state.installation_id,
            legacy_manifest=self._state.legacy_manifest,
            candidate_manifest=self._state.candidate_manifest,
            active_deployment_revision=self._state.legacy_manifest.deployment_revision,
            status="verified",
            legacy_read_only=False,
            candidate_read_only=True,
        )
        self._save(state)
        self._event(DeploymentActivationAuditEvent("deployment_activation_verified", state.active_deployment_revision, candidate_manifest_digest))
        return state

    def activate(self, *, deployment_revision: str) -> DeploymentActivationState:
        if self._state.status != "verified":
            raise DeploymentActivationError("deployment activation requires verification before activation")
        if deployment_revision != self._state.candidate_manifest.deployment_revision:
            raise DeploymentActivationError("deployment revision does not match verified candidate")
        state = DeploymentActivationState(
            installation_id=self._state.installation_id,
            legacy_manifest=self._state.legacy_manifest,
            candidate_manifest=self._state.candidate_manifest,
            active_deployment_revision=deployment_revision,
            status="activated",
            legacy_read_only=True,
            candidate_read_only=False,
        )
        self._save(state)
        self._event(DeploymentActivationAuditEvent("deployment_activation_committed", deployment_revision))
        return state

    def read_context(self) -> DeploymentManifest:
        self._refresh()
        if self._state.status == "pending":
            raise DeploymentActivationError("no active deployment revision")
        if self._state.active_deployment_revision == self._state.candidate_manifest.deployment_revision:
            return self._state.candidate_manifest
        return self._state.legacy_manifest

    def capture_read_context(self) -> DeploymentReadContext:
        manifest = self.read_context()
        return DeploymentReadContext(
            installation_id=self._state.installation_id,
            deployment_revision=manifest.deployment_revision,
            manifest_digest=manifest.manifest_digest(),
        )

    def assert_read_context(self, context: DeploymentReadContext) -> None:
        if not isinstance(context, DeploymentReadContext):
            raise DeploymentActivationError("deployment read context is invalid")
        self._refresh()
        if self._state.status == "pending":
            raise DeploymentActivationError("deployment read context is no longer active")
        active = self.read_context()
        if (
            context.installation_id != self._state.installation_id
            or context.deployment_revision != active.deployment_revision
            or context.manifest_digest != active.manifest_digest()
        ):
            raise DeploymentActivationError("deployment revision changed; retry the read")

    def assert_revision(self, deployment_revision: str) -> None:
        self._refresh()
        if deployment_revision != self._state.active_deployment_revision:
            raise DeploymentActivationError("deployment revision is stale; retry the read")

    def rollback(self, *, reason: str) -> DeploymentActivationState:
        if self._state.status not in {"prepared", "drained", "verified", "activated"}:
            raise DeploymentActivationError("deployment activation has no rollbackable state")
        if not isinstance(reason, str) or not reason.strip():
            raise DeploymentActivationError("deployment rollback reason must not be empty")
        state = DeploymentActivationState(
            installation_id=self._state.installation_id,
            legacy_manifest=self._state.legacy_manifest,
            candidate_manifest=self._state.candidate_manifest,
            active_deployment_revision=self._state.legacy_manifest.deployment_revision,
            status="rolled_back",
            legacy_read_only=False,
            candidate_read_only=True,
        )
        self._save(state)
        self._event(
            DeploymentActivationAuditEvent(
                "deployment_activation_rolled_back",
                state.active_deployment_revision,
                _digest(reason),
            )
        )
        return state


__all__ = [
    "DeploymentActivationAuditEvent",
    "DeploymentActivationController",
    "DeploymentActivationError",
    "DeploymentActivationState",
    "DeploymentActivationStore",
    "DeploymentArtifact",
    "DeploymentManifest",
    "DeploymentReadContext",
]
