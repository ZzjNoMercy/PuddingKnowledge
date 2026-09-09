"""Application ports for artifacts; infrastructure owns their implementations."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from knowledge_contracts import (
    BlobReadRequest,
    BlobReadResult,
    CitationCandidate,
    Evidence,
    TraceEvent,
    validate_blob_read_result,
)


class CitationNormalizer(Protocol):
    def normalize(
        self,
        candidates: Sequence[CitationCandidate],
        *,
        max_items: int = 50,
    ) -> tuple[Evidence, ...]:
        """Convert provider-shaped candidates into portable Evidence."""


class BlobReader(Protocol):
    async def read(self, request: BlobReadRequest) -> BlobReadResult:
        """Read a bounded range by stable resource URI after authorization."""


class VerifiedBlobReader:
    """Application wrapper that validates provider output before returning it."""

    def __init__(self, provider: BlobReader) -> None:
        self._provider = provider

    async def read(self, request: BlobReadRequest) -> BlobReadResult:
        result = await self._provider.read(request)
        validate_blob_read_result(request, result)
        return result


class TraceSink(Protocol):
    async def emit(self, event: TraceEvent) -> None:
        """Persist or stream a digest-only trace event."""
