"""Framework-neutral contracts for a local Platform continuity drill.

The drill models the smallest safe cutover boundary: one deployment revision,
one capability handler, and an explicit legacy rollback switch.  It is not a
production scheduler and deliberately stores only digests in its audit trail.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final, Protocol

_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,160}$")
_URI_RE = re.compile(r"^knowledge://spaces/[A-Za-z0-9._-]{1,160}/[^\s]+$")

CONTINUITY_CAPABILITIES: Final[tuple[str, ...]] = (
    "wiki_compile",
    "capture_processing",
    "connector_sync",
    "gbrain_projection",
    "logical_dataset_processing",
    "semantic_dimension",
)


@dataclass(frozen=True, slots=True)
class ContinuityRequest:
    capability: str
    space_id: str
    resource_key: str
    idempotency_key: str
    deployment_revision: str

    def __post_init__(self) -> None:
        if self.capability not in CONTINUITY_CAPABILITIES:
            raise ValueError("unsupported continuity capability")
        for field_name in ("space_id", "resource_key", "idempotency_key", "deployment_revision"):
            if not _ID_RE.fullmatch(getattr(self, field_name)):
                raise ValueError(f"invalid continuity {field_name}")


@dataclass(frozen=True, slots=True)
class ContinuityResult:
    capability: str
    space_id: str
    resource_uri: str
    deployment_revision: str
    replayed: bool = False

    def __post_init__(self) -> None:
        if self.capability not in CONTINUITY_CAPABILITIES or not _ID_RE.fullmatch(self.space_id):
            raise ValueError("invalid continuity result identity")
        if not _URI_RE.fullmatch(self.resource_uri) or not self.resource_uri.startswith(
            f"knowledge://spaces/{self.space_id}/"
        ):
            raise ValueError("continuity resource URI is outside the request Space")
        if not _ID_RE.fullmatch(self.deployment_revision):
            raise ValueError("invalid continuity result revision")


@dataclass(frozen=True, slots=True)
class ContinuityAuditEvent:
    event_type: str
    capability: str
    space_id: str
    deployment_revision: str
    idempotency_digest: str = ""
    detail_digest: str = ""


class LegacyWorkerControl(Protocol):
    @property
    def enabled(self) -> bool: ...

    def stop(self) -> None: ...

    def start(self) -> None: ...


CapabilityHandler = Callable[[ContinuityRequest], str]
CapabilityHandlers = Mapping[str, CapabilityHandler]
