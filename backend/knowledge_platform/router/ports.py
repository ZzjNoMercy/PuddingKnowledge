"""Framework-neutral contracts for single-engine Knowledge routing."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from knowledge_contracts import CAPABILITIES, Correlation, Principal, QueryResult

_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
_CAPABILITIES = frozenset({"document_rag_query", "wiki_query", "table_query", "database_nl2sql"})
_MAX_QUERY_LENGTH = 512
_MAX_AGE_SECONDS = 7 * 24 * 60 * 60


@dataclass(frozen=True, slots=True)
class KnowledgeQueryRequest:
    """Bounded input for v1 routing; one request selects one engine."""

    query: str
    space_id: str | None = None
    collection_id: str | None = None
    capability_hint: str | None = None
    max_age_seconds: int | None = None
    max_cost_units: int = 10
    limit: int = 10

    def __post_init__(self) -> None:
        if type(self.query) is not str or not self.query.strip() or len(self.query) > _MAX_QUERY_LENGTH:
            raise ValueError("KnowledgeQueryRequest.query is invalid")
        for field_name in ("space_id", "collection_id"):
            value = getattr(self, field_name)
            if value is not None and (type(value) is not str or not _ID_RE.fullmatch(value)):
                raise ValueError(f"KnowledgeQueryRequest.{field_name} is invalid")
        if self.capability_hint is not None and self.capability_hint not in _CAPABILITIES:
            raise ValueError("KnowledgeQueryRequest.capability_hint is invalid")
        if self.max_age_seconds is not None and (
            type(self.max_age_seconds) is not int or not 0 <= self.max_age_seconds <= _MAX_AGE_SECONDS
        ):
            raise ValueError("KnowledgeQueryRequest.max_age_seconds is invalid")
        if type(self.max_cost_units) is not int or not 1 <= self.max_cost_units <= 100:
            raise ValueError("KnowledgeQueryRequest.max_cost_units is invalid")
        if type(self.limit) is not int or not 1 <= self.limit <= 20:
            raise ValueError("KnowledgeQueryRequest.limit is invalid")


@dataclass(frozen=True, slots=True)
class CollectionRoute:
    """Portable Collection facts used by the router, not a provider object."""

    collection_id: str
    space_id: str
    version: str
    capabilities: tuple[str, ...]
    asset_ids: tuple[str, ...] = ()
    semantic_asset_ids: tuple[str, ...] = ()
    freshness: Mapping[str, object] = field(default_factory=dict)
    cost_units: int = 1
    provider_bindings: Mapping[str, Mapping[str, str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for field_name in ("collection_id", "space_id", "version"):
            value = getattr(self, field_name)
            if type(value) is not str or not _ID_RE.fullmatch(value):
                raise ValueError(f"CollectionRoute.{field_name} is invalid")
        if not self.capabilities or any(item not in CAPABILITIES for item in self.capabilities):
            raise ValueError("CollectionRoute.capabilities are invalid")
        if len(set(self.capabilities)) != len(self.capabilities):
            raise ValueError("CollectionRoute.capabilities must be unique")
        if any(type(item) is not str or not _ID_RE.fullmatch(item) for item in self.asset_ids):
            raise ValueError("CollectionRoute.asset_ids are invalid")
        if any(type(item) is not str or not _ID_RE.fullmatch(item) for item in self.semantic_asset_ids):
            raise ValueError("CollectionRoute.semantic_asset_ids are invalid")
        if not isinstance(self.freshness, Mapping):
            raise ValueError("CollectionRoute.freshness must be an object")
        if type(self.cost_units) is not int or not 1 <= self.cost_units <= 100:
            raise ValueError("CollectionRoute.cost_units is invalid")
        if not isinstance(self.provider_bindings, Mapping):
            raise ValueError("CollectionRoute.provider_bindings must be an object")
        for capability, binding in self.provider_bindings.items():
            if type(capability) is not str or capability not in _CAPABILITIES or not isinstance(binding, Mapping):
                raise ValueError("CollectionRoute.provider_bindings are invalid")
            if set(binding) not in ({"asset_id"}, {"dataset_id"}, {"provider_id"}):
                raise ValueError("CollectionRoute.provider_bindings must select one explicit identity")
            identity = next(iter(binding.values()))
            if type(identity) is not str or not _ID_RE.fullmatch(identity):
                raise ValueError("CollectionRoute.provider_bindings identity is invalid")

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> CollectionRoute:
        if not isinstance(record, Mapping):
            raise ValueError("Collection record must be an object")
        capabilities = record.get("capabilities", ())
        asset_ids = record.get("asset_ids", ())
        semantic_asset_ids = record.get("semantic_asset_ids", ())
        freshness = record.get("freshness", {})
        if (
            not isinstance(capabilities, (list, tuple))
            or not isinstance(asset_ids, (list, tuple))
            or not isinstance(semantic_asset_ids, (list, tuple))
        ):
            raise ValueError("Collection record list fields are invalid")
        if not isinstance(freshness, Mapping):
            raise ValueError("Collection record freshness is invalid")
        raw_cost = record.get("cost_units", freshness.get("cost_units", 1))
        if type(raw_cost) is not int:
            raise ValueError("Collection record cost_units is invalid")
        provider_bindings = record.get("provider_bindings", {})
        if not isinstance(provider_bindings, Mapping):
            raise ValueError("Collection record provider_bindings is invalid")
        if any(not isinstance(binding, Mapping) for binding in provider_bindings.values()):
            raise ValueError("Collection record provider_bindings contains an invalid binding")
        return cls(
            collection_id=record.get("id", ""),
            space_id=record.get("space_id", ""),
            version=record.get("version", ""),
            capabilities=tuple(capabilities),
            asset_ids=tuple(asset_ids),
            semantic_asset_ids=tuple(semantic_asset_ids),
            freshness=dict(freshness),
            cost_units=raw_cost,
            provider_bindings={
                capability: dict(binding)
                for capability, binding in provider_bindings.items()
            },
        )


class CollectionRouteCatalog(Protocol):
    """Read-only Collection binding source."""

    def list_collections(self, *, space_id: str | None = None) -> Sequence[Mapping[str, object]]: ...


class KnowledgeQueryEngine(Protocol):
    """One engine invocation. Implementations must not combine other engines."""

    async def query(
        self,
        *,
        request: KnowledgeQueryRequest,
        collection: CollectionRoute,
        principal: Principal,
        correlation: Correlation,
    ) -> QueryResult: ...
