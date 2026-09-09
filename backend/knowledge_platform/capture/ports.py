"""Framework-neutral ports for local Web Capture processing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from knowledge_contracts import is_valid_knowledge_uri
from knowledge_platform.wiki.ports import RawSnapshot


@dataclass(frozen=True, slots=True)
class CaptureProcessingRequest:
    asset_id: str
    source_revision: str
    source_uri: str
    content_digest: str
    idempotency_key: str

    def __post_init__(self) -> None:
        for field_name in ("asset_id", "source_revision", "source_uri", "content_digest", "idempotency_key"):
            if not getattr(self, field_name).strip():
                raise ValueError(f"CaptureProcessingRequest.{field_name} must not be empty")
        if not is_valid_knowledge_uri(self.source_uri):
            raise ValueError("CaptureProcessingRequest.source_uri must use knowledge://")


@dataclass(frozen=True, slots=True)
class CaptureProcessingResult:
    asset_id: str
    source_revision: str
    resource_uri: str

    def __post_init__(self) -> None:
        if not self.asset_id.strip() or not self.source_revision.strip():
            raise ValueError("CaptureProcessingResult identity must not be empty")
        if not is_valid_knowledge_uri(self.resource_uri):
            raise ValueError("CaptureProcessingResult.resource_uri must use knowledge://")


class CaptureSnapshotRepository(Protocol):
    async def get(self, *, snapshot_id: str, source_revision: str) -> RawSnapshot: ...


class CapturePublishingService(Protocol):
    async def publish(self, *, snapshot: RawSnapshot, idempotency_key: str) -> str: ...


class CaptureProcessingJobStore(Protocol):
    async def claim(self, *, idempotency_key: str): ...

    async def complete(self, *, idempotency_key: str, resource_uri: str) -> None: ...

    async def release(self, *, idempotency_key: str) -> None: ...
