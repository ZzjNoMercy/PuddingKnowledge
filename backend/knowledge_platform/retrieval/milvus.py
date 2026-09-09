"""Provider-neutral Milvus adapter with Catalog identity and revision fencing."""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from knowledge_contracts import CitationCandidate
from knowledge_platform.catalog.query import CatalogQueryRepository
from knowledge_platform.catalog.vector_rebuild import VectorRebuildManifest

from .ports import RetrievalIndexNotReady, RetrievalProviderError

_COLLECTION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_MAX_QUERY_LENGTH = 512
_MAX_LIMIT = 50
_PHYSICAL_PATH_RE = re.compile(
    r"(?i)(?:(?<![A-Za-z0-9])/(?:Users|private|tmp|var|home|opt|etc|Volumes|Applications)/[^\s<>\"']+|(?<![A-Za-z0-9])[A-Za-z]:[\\/][^\s<>\"']+|\\\\[^\s<>\"']+)"
)
_FILE_URI_RE = re.compile(r"(?i)file://[^\s<>\"']+")


def _hit_value(hit: object, key: str) -> object:
    if isinstance(hit, Mapping):
        if key in hit:
            return hit[key]
        entity = hit.get("entity")
        if isinstance(entity, Mapping):
            return entity.get(key)
    value = getattr(hit, key, None)
    if value is not None:
        return value
    entity = getattr(hit, "entity", None)
    return entity.get(key) if isinstance(entity, Mapping) else None


def _bounded_score(value: object) -> float:
    if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        return 0.0
    return max(0.0, min(1.0, float(value)))


def _safe_quote(value: str) -> str:
    """Remove physical file references before text reaches the citation contract."""

    return _FILE_URI_RE.sub("[redacted-path]", _PHYSICAL_PATH_RE.sub("[redacted-path]", value[:1200]))


class MilvusCatalogRetrievalProvider:
    """Search a candidate index whose rows are bound to Catalog Asset IDs.

    ``client`` and ``encode_query`` are injected ports.  The Platform package
    therefore does not import legacy indexers, credentials, or model config.
    """

    def __init__(
        self,
        *,
        catalog: CatalogQueryRepository,
        client: Any,
        collection_name: str,
        manifest: VectorRebuildManifest,
        encode_query: Callable[[str], Sequence[float]],
        vector_field: str = "embedding",
    ) -> None:
        if not isinstance(collection_name, str) or not _COLLECTION_RE.fullmatch(collection_name):
            raise ValueError("Milvus collection name is invalid")
        if collection_name != manifest.provider_collection_name:
            raise ValueError("Milvus collection is not the manifest candidate")
        if not isinstance(vector_field, str) or not _COLLECTION_RE.fullmatch(vector_field):
            raise ValueError("Milvus vector field is invalid")
        self._catalog = catalog
        self._client = client
        self._collection_name = collection_name
        self._manifest = manifest
        self._encode_query = encode_query
        self._vector_field = vector_field
        self._asset_ids = frozenset(item.asset_id for item in manifest.documents)

    async def search(
        self, *, query: str, space_id: str | None, limit: int
    ) -> tuple[CitationCandidate, ...]:
        if not isinstance(query, str) or not query.strip() or len(query) > _MAX_QUERY_LENGTH:
            raise RetrievalProviderError("retrieval query is invalid")
        if type(limit) is not int or not 0 < limit <= _MAX_LIMIT:
            raise RetrievalProviderError("retrieval limit is invalid")
        if space_id is not None and space_id != self._manifest.space_id:
            return ()
        try:
            vector = list(self._encode_query(query))
        except Exception as error:  # provider boundary must normalize encoder failures
            raise RetrievalProviderError("query encoder is unavailable") from error
        if not vector or any(type(item) not in (int, float) or not math.isfinite(float(item)) for item in vector):
            raise RetrievalProviderError("query encoder returned an invalid vector")
        try:
            raw_results = self._client.search(
                collection_name=self._collection_name,
                data=[vector],
                anns_field=self._vector_field,
                limit=limit,
                output_fields=["doc_id", "text"],
            )
        except Exception as error:
            raise RetrievalProviderError("Milvus retrieval is unavailable") from error
        if not isinstance(raw_results, list):
            raise RetrievalProviderError("Milvus retrieval returned an invalid shape")
        hits = raw_results[0] if raw_results else []
        if not isinstance(hits, list):
            raise RetrievalProviderError("Milvus retrieval returned an invalid hit list")

        candidates: list[CitationCandidate] = []
        seen: set[str] = set()
        for hit in hits:
            asset_id = _hit_value(hit, "doc_id")
            if not isinstance(asset_id, str) or asset_id not in self._asset_ids:
                raise RetrievalIndexNotReady("Milvus row is not bound to the candidate Catalog manifest")
            if asset_id in seen:
                continue
            asset = self._catalog.get_asset(asset_id=asset_id)
            if asset is None or str(asset.get("space_id") or "") != self._manifest.space_id:
                raise RetrievalIndexNotReady("Milvus row resolves outside the candidate Catalog Space")
            resource_uri = str(asset.get("source_uri") or "")
            text = _hit_value(hit, "text")
            if not isinstance(text, str):
                text = ""
            numeric_score = _bounded_score(_hit_value(hit, "distance"))
            chunk_id = _hit_value(hit, "id")
            if not isinstance(chunk_id, str) or not chunk_id:
                chunk_id = f"{asset_id}:unknown"
            try:
                candidate = CitationCandidate(
                    asset_id=asset_id,
                    resource_uri=resource_uri,
                    quote=_safe_quote(text),
                    locator={"chunk_id": chunk_id},
                    score=numeric_score,
                )
            except ValueError as error:
                raise RetrievalProviderError("Milvus result contains unsafe citation text") from error
            candidates.append(candidate)
            seen.add(asset_id)
            if len(candidates) >= limit:
                break
        return tuple(candidates)


class MilvusBm25CatalogRetrievalProvider:
    """Search a candidate Milvus BM25 function bound to Catalog Asset IDs."""

    def __init__(
        self,
        *,
        catalog: CatalogQueryRepository,
        client: Any,
        collection_name: str,
        manifest: VectorRebuildManifest,
    ) -> None:
        if not isinstance(collection_name, str) or not _COLLECTION_RE.fullmatch(collection_name):
            raise ValueError("Milvus collection name is invalid")
        if collection_name != manifest.provider_collection_name:
            raise ValueError("Milvus collection is not the manifest candidate")
        self._catalog = catalog
        self._client = client
        self._collection_name = collection_name
        self._manifest = manifest
        self._asset_ids = frozenset(item.asset_id for item in manifest.documents)

    async def search(
        self, *, query: str, space_id: str | None, limit: int
    ) -> tuple[CitationCandidate, ...]:
        if not isinstance(query, str) or not query.strip() or len(query) > _MAX_QUERY_LENGTH:
            raise RetrievalProviderError("retrieval query is invalid")
        if type(limit) is not int or not 0 < limit <= _MAX_LIMIT:
            raise RetrievalProviderError("retrieval limit is invalid")
        if space_id is not None and space_id != self._manifest.space_id:
            return ()
        try:
            raw_results = self._client.search(
                collection_name=self._collection_name,
                data=[query],
                anns_field="sparse_embedding",
                limit=limit,
                output_fields=["doc_id", "text"],
            )
        except Exception as error:
            raise RetrievalProviderError("Milvus BM25 retrieval is unavailable") from error
        if not isinstance(raw_results, list):
            raise RetrievalProviderError("Milvus BM25 retrieval returned an invalid shape")
        hits = raw_results[0] if raw_results else []
        if not isinstance(hits, list):
            raise RetrievalProviderError("Milvus BM25 retrieval returned an invalid hit list")
        candidates: list[CitationCandidate] = []
        seen: set[str] = set()
        for hit in hits:
            asset_id = _hit_value(hit, "doc_id")
            if not isinstance(asset_id, str) or asset_id not in self._asset_ids:
                raise RetrievalIndexNotReady("Milvus BM25 row is not bound to the candidate Catalog manifest")
            if asset_id in seen:
                continue
            asset = self._catalog.get_asset(asset_id=asset_id)
            if asset is None or str(asset.get("space_id") or "") != self._manifest.space_id:
                raise RetrievalIndexNotReady("Milvus BM25 row resolves outside the candidate Catalog Space")
            text = _hit_value(hit, "text")
            numeric_score = _bounded_score(_hit_value(hit, "distance"))
            chunk_id = _hit_value(hit, "id")
            if not isinstance(chunk_id, str) or not chunk_id:
                chunk_id = f"{asset_id}:unknown"
            try:
                candidate = CitationCandidate(
                    asset_id=asset_id,
                    resource_uri=str(asset.get("source_uri") or ""),
                    quote=_safe_quote(text) if isinstance(text, str) else "",
                    locator={"chunk_id": chunk_id},
                    score=numeric_score,
                )
            except ValueError as error:
                raise RetrievalProviderError("Milvus BM25 result contains unsafe citation text") from error
            candidates.append(candidate)
            seen.add(asset_id)
            if len(candidates) >= limit:
                break
        return tuple(candidates)


__all__ = ["MilvusBm25CatalogRetrievalProvider", "MilvusCatalogRetrievalProvider"]
