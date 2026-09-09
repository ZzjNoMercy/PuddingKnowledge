"""Ports and DTOs for Wiki compilation.

These types intentionally know nothing about public Tool objects, MCP, graph
state, attachments, or a particular model SDK.  Infrastructure adapters may
implement the ports; the application worker only coordinates them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from knowledge_contracts import is_valid_knowledge_uri


@dataclass(frozen=True, slots=True)
class RawSnapshot:
    snapshot_id: str
    source_revision: str
    source_uri: str
    content: str
    content_digest: str

    def __post_init__(self) -> None:
        for field_name in ("snapshot_id", "source_revision", "source_uri", "content_digest"):
            if not getattr(self, field_name).strip():
                raise ValueError(f"RawSnapshot.{field_name} must not be empty")
        if not is_valid_knowledge_uri(self.source_uri):
            raise ValueError("RawSnapshot.source_uri must use knowledge://")


@dataclass(frozen=True, slots=True)
class WikiDraft:
    path: str
    title: str
    markdown: str
    source_snapshot_id: str
    source_revision: str

    def __post_init__(self) -> None:
        normalized = self.path.replace("\\", "/")
        if (
            not self.path.strip()
            or normalized != self.path
            or normalized.startswith(("/", "~", "//"))
            or ":" in normalized
            or "\x00" in normalized
            or any(segment in {"", ".", ".."} for segment in normalized.split("/"))
        ):
            raise ValueError("WikiDraft.path must be a relative workspace path")
        if not self.title.strip() or not self.markdown.strip():
            raise ValueError("WikiDraft title and markdown must not be empty")
        if not self.source_snapshot_id.strip() or not self.source_revision.strip():
            raise ValueError("WikiDraft source identity must not be empty")


@dataclass(frozen=True, slots=True)
class WikiValidationResult:
    valid: bool
    errors: tuple[str, ...] = ()
    receipt_id: str = ""

    def __post_init__(self) -> None:
        if self.valid and (self.errors or not self.receipt_id.strip()):
            raise ValueError("valid WikiValidationResult needs no errors and a receipt")
        if not self.valid and not self.errors:
            raise ValueError("invalid WikiValidationResult must contain errors")


@dataclass(frozen=True, slots=True)
class ValidatedWikiDraft:
    """A draft paired with the validator's non-empty receipt."""

    draft: WikiDraft
    receipt_id: str

    def __post_init__(self) -> None:
        if not self.receipt_id.strip():
            raise ValueError("ValidatedWikiDraft.receipt_id must not be empty")


@dataclass(frozen=True, slots=True)
class WikiCompilationClaim:
    acquired: bool
    existing_resource_uri: str | None = None

    def __post_init__(self) -> None:
        if self.acquired and self.existing_resource_uri is not None:
            raise ValueError("Acquired compilation claim cannot contain an existing resource")
        if self.existing_resource_uri is not None and not is_valid_knowledge_uri(self.existing_resource_uri):
            raise ValueError("Existing Wiki resource must use knowledge://")


class RawSnapshotRepository(Protocol):
    async def get(self, *, snapshot_id: str, source_revision: str) -> RawSnapshot:
        """Read one immutable raw snapshot."""


class WikiContextService(Protocol):
    async def build_context(self, snapshot: RawSnapshot) -> str:
        """Build bounded compiler context from the raw snapshot."""


class ModelGateway(Protocol):
    async def generate(self, *, context: str, snapshot: RawSnapshot) -> WikiDraft:
        """Generate a draft without publishing or mutating Platform state."""


class WikiDraftValidator(Protocol):
    async def validate(self, draft: WikiDraft, *, snapshot: RawSnapshot) -> WikiValidationResult:
        """Lint schema, citations, source linkage and policy constraints."""


class WikiPublishingService(Protocol):
    async def publish(
        self,
        draft: ValidatedWikiDraft,
        *,
        snapshot: RawSnapshot,
        idempotency_key: str,
    ) -> str:
        """Idempotently publish only a validated draft and return its stable URI."""


class WikiCompilationJobStore(Protocol):
    async def claim(self, *, idempotency_key: str) -> WikiCompilationClaim:
        """Atomically claim one compilation or return its terminal resource."""

    async def complete(self, *, idempotency_key: str, resource_uri: str) -> None:
        """Record the terminal published resource for an idempotency key."""

    async def release(self, *, idempotency_key: str) -> None:
        """Release a failed claim so a later retry can safely re-run it."""
