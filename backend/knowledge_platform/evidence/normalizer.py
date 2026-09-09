"""Deterministic reference implementation for citation normalization."""

from __future__ import annotations

from collections.abc import Sequence

from knowledge_contracts import CitationCandidate, Evidence


class DeterministicCitationNormalizer:
    """Deduplicate and bound citations without importing a retrieval provider."""

    def normalize(
        self,
        candidates: Sequence[CitationCandidate],
        *,
        max_items: int = 50,
    ) -> tuple[Evidence, ...]:
        if max_items <= 0:
            raise ValueError("max_items must be positive")
        result: list[Evidence] = []
        seen: set[tuple[str, str, tuple[tuple[str, str], ...]]] = set()
        for candidate in candidates:
            locator = tuple(sorted((str(key), str(value)) for key, value in candidate.locator.items()))
            identity = (candidate.asset_id, candidate.resource_uri, locator)
            if identity in seen:
                continue
            seen.add(identity)
            result.append(
                Evidence(
                    asset_id=candidate.asset_id,
                    resource_uri=candidate.resource_uri,
                    locator=dict(locator),
                    quote=candidate.quote,
                    score=candidate.score,
                )
            )
            if len(result) >= max_items:
                break
        return tuple(result)
