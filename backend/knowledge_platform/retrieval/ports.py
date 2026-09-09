"""Provider ports for read-only document and published Wiki retrieval."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from knowledge_contracts import CitationCandidate


class RetrievalProviderError(RuntimeError):
    """A provider could not complete a read-only retrieval request."""


class RetrievalIndexNotReady(RetrievalProviderError):
    """The provider has not published a usable index for the requested scope."""


class RetrievalProvider(Protocol):
    async def search(
        self, *, query: str, space_id: str | None, limit: int
    ) -> Sequence[CitationCandidate]:
        """Return provider-shaped candidates; no provider object crosses the boundary."""


class QueryResultScopeReader(Protocol):
    """Read an explicit Platform-owned QueryResult-to-Space binding."""

    def get_space_id(self, *, query_result_id: str) -> str | None: ...
