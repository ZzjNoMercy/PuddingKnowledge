"""Framework-neutral read-only retrieval application services."""

from __future__ import annotations

import base64
import re
from collections.abc import Mapping

from knowledge_contracts import (
    BlobReadRequest,
    CitationCandidate,
    Correlation,
    Evidence,
    Principal,
    Provenance,
    QueryError,
    QueryErrorCode,
    QueryResult,
    validate_blob_read_result,
)
from knowledge_contracts.artifacts import MAX_BLOB_READ_BYTES
from knowledge_platform.catalog.query import CatalogQueryRepository
from knowledge_platform.catalog.service import CatalogQueryService
from knowledge_platform.evidence.normalizer import DeterministicCitationNormalizer
from knowledge_platform.evidence.ports import BlobReader, CitationNormalizer

from .ports import QueryResultScopeReader, RetrievalIndexNotReady, RetrievalProvider, RetrievalProviderError

_MAX_QUERY_LENGTH = 512
_MAX_RETRIEVAL_LIMIT = 50
_OPAQUE_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
_UNSAFE_METADATA_RE = re.compile(
    r"(?i)(?:bearer\s+|(?:password|token|secret|credential)\s*[=:]|api[_-]?key\s*[=:]|access[_-]?token\s*[=:]|private[_-]?key\s*[=:]|"
    r"file://|/(?:Users|private|tmp|var|home|etc|opt|usr|root|mnt|Applications|System|Volumes)/|"
    r"(?<![A-Za-z0-9_])[A-Za-z]:[\\/]|\\\\|\bsk-[A-Za-z0-9._-]{8,})"
)
_UNSAFE_METADATA_KEY_RE = re.compile(r"(?i)(?:password|token|secret|credential|authorization|api[_-]?key|private[_-]?key)")


def _error(correlation: Correlation, code: QueryErrorCode, message: str) -> QueryResult:
    return QueryResult(
        status="error",
        trace_id=correlation.trace_id,
        error=QueryError(code=code, message=message),
    )


def _authorized(principal: Principal) -> bool:
    """Tenant-scoped authorization remains fail-closed until Catalog bindings exist."""

    return principal.tenant_id is None and (
        "knowledge.search" in principal.scopes
        or "knowledge:search" in principal.scopes
        or "knowledge.admin" in principal.scopes
        or "knowledge:admin" in principal.scopes
    )


def _authorized_for_space(principal: Principal, space_id: str | None, *, operation: str) -> bool:
    if principal.tenant_id is not None:
        return False
    if operation == "read":
        has_operation = bool(
            {"knowledge.read", "knowledge:read", "knowledge.admin", "knowledge:admin"} & set(principal.scopes)
        )
    else:
        has_operation = _authorized(principal)
    if not has_operation:
        return False
    return (
        space_id is None
        or "knowledge.admin" in principal.scopes
        or "knowledge:admin" in principal.scopes
        or f"knowledge.space:{space_id}" in principal.scopes
        or f"knowledge:space:{space_id}" in principal.scopes
    )


def _valid_digest(value: object) -> str | None:
    candidate = str(value or "")
    return candidate if re.fullmatch(r"sha256:[0-9a-f]{64}", candidate) else None


def _query_error_for_provider(correlation: Correlation, error: Exception) -> QueryResult:
    if isinstance(error, RetrievalIndexNotReady):
        return _error(correlation, QueryErrorCode.INDEX_NOT_READY, "Retrieval index is not ready")
    if isinstance(error, RetrievalProviderError):
        return _error(correlation, QueryErrorCode.CAPABILITY_UNAVAILABLE, "Retrieval provider is unavailable")
    return _error(correlation, QueryErrorCode.INTERNAL_ERROR, "Retrieval query failed")


def _unsafe_metadata(value: object) -> bool:
    if isinstance(value, str):
        return bool(_UNSAFE_METADATA_RE.search(value))
    if isinstance(value, Mapping):
        return any(
            bool(_UNSAFE_METADATA_KEY_RE.search(str(key))) or _unsafe_metadata(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_unsafe_metadata(item) for item in value)
    return False


class CatalogSearchService:
    """Expose metadata/Portal Search through the existing Catalog read service."""

    def __init__(self, catalog: CatalogQueryService) -> None:
        self._catalog = catalog

    def search_portal(self, **kwargs: object) -> QueryResult:
        result = self._catalog.search_assets(**kwargs)
        if result.status == "error":
            return result
        if (
            _unsafe_metadata(result.data)
            or (
                result.provenance is not None
                and (
                    _unsafe_metadata(result.provenance.space_id)
                    or _unsafe_metadata(result.provenance.dataset_id)
                    or _unsafe_metadata(result.provenance.dataset_version)
                    or _unsafe_metadata(result.provenance.capability)
                    or _unsafe_metadata(result.provenance.provider_versions)
                )
            )
            or any(_unsafe_metadata(warning.message) or _unsafe_metadata(warning.details) for warning in result.warnings)
        ):
            correlation = kwargs.get("correlation")
            if not isinstance(correlation, Correlation):
                correlation = Correlation("portal_search")
            return _error(correlation, QueryErrorCode.INTERNAL_ERROR, "Catalog metadata is not portable")
        return QueryResult(
            status="ok",
            trace_id=result.trace_id,
            data={**result.data, "channel": "metadata"},
            evidence=result.evidence,
            provenance=result.provenance,
            warnings=result.warnings,
        )


class AssetReadService:
    """Read a bounded stable URI through a verified BlobReader."""

    def __init__(
        self,
        *,
        catalog: CatalogQueryRepository,
        reader: BlobReader,
    ) -> None:
        self._catalog = catalog
        self._reader = reader

    async def read(
        self,
        *,
        principal: Principal,
        correlation: Correlation,
        resource_uri: str,
        start: int = 0,
        end: int | None = None,
        expected_digest: str | None = None,
    ) -> QueryResult:
        if not ({"knowledge.read", "knowledge:read", "knowledge.admin", "knowledge:admin"} & set(principal.scopes)):
            return _error(correlation, QueryErrorCode.PERMISSION_DENIED, "knowledge.read scope is required")
        if principal.tenant_id is not None:
            return _error(correlation, QueryErrorCode.PERMISSION_DENIED, "tenant-scoped Asset reads are unavailable")
        try:
            request = BlobReadRequest(
                resource_uri=resource_uri,
                principal=principal,
                correlation=correlation,
                start=start,
                end=end,
                expected_digest=expected_digest,
            )
        except (TypeError, ValueError):
            return _error(correlation, QueryErrorCode.INVALID_REQUEST, "Asset read request is invalid")
        uri_parts = resource_uri.removeprefix("knowledge://").split("/")
        if len(uri_parts) != 4 or uri_parts[0] != "spaces" or uri_parts[2] != "assets":
            return _error(correlation, QueryErrorCode.INVALID_REQUEST, "Asset URI shape is invalid")
        asset_id = uri_parts[3]
        space_id = uri_parts[1]
        if not _authorized_for_space(principal, space_id, operation="read"):
            return _error(correlation, QueryErrorCode.PERMISSION_DENIED, "Asset Space scope is required")
        try:
            revision_before = _valid_digest(self._catalog.catalog_revision)
        except Exception:
            revision_before = None
        if revision_before is None:
            return _error(correlation, QueryErrorCode.BINDING_UNAVAILABLE, "Catalog revision is unavailable")
        try:
            asset = self._catalog.get_asset(asset_id=asset_id)
        except Exception:
            return _error(correlation, QueryErrorCode.INTERNAL_ERROR, "Catalog asset binding failed")
        if (
            asset is None
            or str(asset.get("id") or "") != asset_id
            or str(asset.get("source_uri") or "") != resource_uri
            or str(asset.get("space_id") or "") != space_id
        ):
            return _error(correlation, QueryErrorCode.NOT_FOUND, "Asset was not found")
        catalog_digest = _valid_digest(asset.get("content_digest"))
        if catalog_digest is None:
            return _error(correlation, QueryErrorCode.BINDING_UNAVAILABLE, "Asset content digest is unavailable")
        try:
            result = await self._reader.read(request)
            validate_blob_read_result(request, result)
        except (TypeError, ValueError):
            return _error(correlation, QueryErrorCode.INTERNAL_ERROR, "Asset reader returned an invalid result")
        except Exception:
            return _error(correlation, QueryErrorCode.INTERNAL_ERROR, "Asset read failed")
        if catalog_digest != (result.asset_digest or result.content_digest):
            return _error(correlation, QueryErrorCode.INTERNAL_ERROR, "Asset content changed from Catalog binding")
        try:
            revision_after = _valid_digest(self._catalog.catalog_revision)
        except Exception:
            revision_after = None
        if revision_after is None or revision_after != revision_before:
            return _error(correlation, QueryErrorCode.INTERNAL_ERROR, "Catalog changed during Asset read")
        try:
            evidence = Evidence(
                asset_id=asset_id,
                resource_uri=resource_uri,
                locator={"chunk_id": f"bytes:{result.start}-{result.end}"},
                revision=result.content_digest,
                matched_by=("resource_uri",),
            )
        except ValueError:
            return _error(correlation, QueryErrorCode.INTERNAL_ERROR, "Asset URI cannot produce evidence")
        return QueryResult(
            status="ok",
            trace_id=correlation.trace_id,
            data={
                "resource_uri": result.resource_uri,
                "start": result.start,
                "end": result.end,
                "content_digest": result.content_digest,
                "asset_digest": result.asset_digest or result.content_digest,
                "content_base64": base64.b64encode(result.content).decode("ascii"),
                "mime_type": str(asset.get("mime_type") or "application/octet-stream"),
                "truncated": result.end - result.start >= MAX_BLOB_READ_BYTES,
                "next_locator": {"start": result.end}
                if result.end - result.start >= MAX_BLOB_READ_BYTES
                else None,
            },
            evidence=(evidence,),
            provenance=Provenance(
                space_id=space_id,
                dataset_id=None,
                dataset_version=None,
                capability="knowledge_read",
                catalog_revision=revision_before,
            ),
        )


class AssetDerivativeService:
    """Expose only explicitly bound logical derivatives of an Asset.

    A derivative registry is supplied by the hosting process.  The Catalog
    never infers a file path or treats a basename as a binding.  This keeps a
    local published Markdown file usable as ``normalized_markdown`` while
    making an unbound derivative absent from the public surface.
    """

    def __init__(
        self,
        *,
        catalog: CatalogQueryService,
        asset_read: AssetReadService,
        bindings: Mapping[str, tuple[str, ...]],
    ) -> None:
        self._catalog = catalog
        self._asset_read = asset_read
        self._bindings = {
            str(asset_id): tuple(str(kind) for kind in kinds)
            for asset_id, kinds in bindings.items()
        }

    @staticmethod
    def _uri(asset: Mapping[str, object], kind: str) -> str:
        base_uri = str(asset.get("source_uri") or "")
        return f"{base_uri}/derivatives/{kind}"

    def _metadata(
        self, *, principal: Principal, correlation: Correlation, asset_id: str
    ) -> tuple[QueryResult, Mapping[str, object] | None]:
        metadata = self._catalog.read_asset(
            principal=principal, correlation=correlation, asset_id=asset_id
        )
        if metadata.status == "error":
            return metadata, None
        asset = metadata.data.get("asset")
        if not isinstance(asset, Mapping):
            return _error(correlation, QueryErrorCode.INTERNAL_ERROR, "Catalog asset metadata is malformed"), None
        return metadata, asset

    def list(
        self, *, principal: Principal, correlation: Correlation, asset_id: str
    ) -> QueryResult:
        metadata, asset = self._metadata(
            principal=principal, correlation=correlation, asset_id=asset_id
        )
        if metadata.status == "error" or asset is None:
            return metadata
        revision = _valid_digest(asset.get("revision")) or _valid_digest(asset.get("content_digest"))
        if revision is None:
            return _error(correlation, QueryErrorCode.BINDING_UNAVAILABLE, "Asset revision is unavailable")
        derivatives = [
            {
                "asset_id": asset_id,
                "kind": kind,
                "resource_uri": self._uri(asset, kind),
                "revision": revision,
                "content_digest": _valid_digest(asset.get("content_digest")) or revision,
                "mime_type": str(asset.get("mime_type") or "application/octet-stream"),
            }
            for kind in self._bindings.get(asset_id, ())
        ]
        return QueryResult(
            status="ok",
            trace_id=correlation.trace_id,
            data={"derivatives": derivatives, "count": len(derivatives)},
            provenance=metadata.provenance,
        )

    async def read(
        self,
        *,
        principal: Principal,
        correlation: Correlation,
        asset_id: str,
        kind: str,
        start: int = 0,
        end: int | None = None,
        expected_digest: str | None = None,
    ) -> QueryResult:
        metadata, asset = self._metadata(
            principal=principal, correlation=correlation, asset_id=asset_id
        )
        if metadata.status == "error" or asset is None:
            return metadata
        if type(kind) is not str or not _OPAQUE_ID_RE.fullmatch(kind):
            return _error(correlation, QueryErrorCode.INVALID_REQUEST, "derivative kind is invalid")
        if kind not in self._bindings.get(asset_id, ()):
            return _error(correlation, QueryErrorCode.NOT_FOUND, "Asset derivative was not found")
        base_uri = str(asset.get("source_uri") or "")
        result = await self._asset_read.read(
            principal=principal,
            correlation=correlation,
            resource_uri=base_uri,
            start=start,
            end=MAX_BLOB_READ_BYTES if end is None else end,
            expected_digest=expected_digest,
        )
        if result.status == "error":
            return result
        derivative_uri = self._uri(asset, kind)
        try:
            payload = dict(result.data)
            payload["resource_uri"] = derivative_uri
            payload["derivative"] = kind
            evidence = Evidence(
                asset_id=asset_id,
                resource_uri=derivative_uri,
                locator={"chunk_id": f"bytes:{payload['start']}-{payload['end']}"},
                revision=str(payload["content_digest"]),
                matched_by=("derivative",),
            )
        except (KeyError, TypeError, ValueError):
            return _error(correlation, QueryErrorCode.INTERNAL_ERROR, "Asset derivative is not portable")
        return QueryResult(
            status="ok",
            trace_id=result.trace_id,
            data=payload,
            evidence=(evidence,),
            provenance=result.provenance,
        )


class QueryResultArtifactReadService:
    """Read a QueryResult artifact after Catalog and host binding verification."""

    _ARTIFACT_URI_RE = re.compile(
        r"^knowledge://query-results/([A-Za-z0-9._:-]{1,160})/artifact$"
    )

    def __init__(
        self,
        *,
        catalog: CatalogQueryRepository,
        reader: BlobReader,
        scope_reader: QueryResultScopeReader | None = None,
    ) -> None:
        self._catalog = catalog
        self._reader = reader
        self._scope_reader = scope_reader

    @property
    def resource_template_available(self) -> bool:
        """Whether the host supplied the mandatory Space binding reader."""

        return self._scope_reader is not None

    async def read(
        self,
        *,
        principal: Principal,
        correlation: Correlation,
        query_result_id: str,
        resource_uri: str,
        start: int = 0,
        end: int | None = None,
        expected_digest: str | None = None,
    ) -> QueryResult:
        if not ("knowledge.read" in principal.scopes or "knowledge:read" in principal.scopes
                or "knowledge.admin" in principal.scopes or "knowledge:admin" in principal.scopes):
            return _error(correlation, QueryErrorCode.PERMISSION_DENIED, "knowledge.read scope is required")
        if principal.tenant_id is not None:
            return _error(correlation, QueryErrorCode.PERMISSION_DENIED, "tenant-scoped QueryResult reads are unavailable")
        match = self._ARTIFACT_URI_RE.fullmatch(resource_uri)
        if (
            type(query_result_id) is not str
            or not _OPAQUE_ID_RE.fullmatch(query_result_id)
            or match is None
            or match.group(1) != query_result_id
        ):
            return _error(correlation, QueryErrorCode.INVALID_REQUEST, "QueryResult artifact URI is invalid")
        try:
            metadata = self._catalog.get_query_result(query_result_id=query_result_id)
        except Exception:
            return _error(correlation, QueryErrorCode.INTERNAL_ERROR, "QueryResult Catalog binding failed")
        if metadata is None:
            return _error(correlation, QueryErrorCode.NOT_FOUND, "QueryResult was not found")
        if str(metadata.get("artifact_uri") or "") != resource_uri:
            return _error(correlation, QueryErrorCode.BINDING_UNAVAILABLE, "QueryResult artifact URI is not bound")
        profile = metadata.get("profile")
        if not isinstance(profile, Mapping):
            return _error(correlation, QueryErrorCode.BINDING_UNAVAILABLE, "QueryResult artifact digest is unavailable")
        full_digest = _valid_digest(profile.get("_artifact_sha256"))
        if full_digest is None:
            return _error(correlation, QueryErrorCode.BINDING_UNAVAILABLE, "QueryResult artifact digest is unavailable")
        if self._scope_reader is None:
            return _error(correlation, QueryErrorCode.BINDING_UNAVAILABLE, "QueryResult Space binding is unavailable")
        try:
            space_id = self._scope_reader.get_space_id(query_result_id=query_result_id)
        except Exception:
            return _error(correlation, QueryErrorCode.BINDING_UNAVAILABLE, "QueryResult Space binding is unavailable")
        if space_id is None or not _OPAQUE_ID_RE.fullmatch(space_id):
            return _error(correlation, QueryErrorCode.BINDING_UNAVAILABLE, "QueryResult Space binding is unavailable")
        if not _authorized_for_space(principal, space_id, operation="read"):
            return _error(correlation, QueryErrorCode.PERMISSION_DENIED, "QueryResult Space scope is required")
        try:
            request = BlobReadRequest(
                resource_uri=resource_uri,
                principal=principal,
                correlation=correlation,
                start=start,
                end=MAX_BLOB_READ_BYTES if end is None else end,
                expected_digest=expected_digest,
            )
            result = await self._reader.read(request)
            validate_blob_read_result(request, result)
        except (TypeError, ValueError):
            return _error(correlation, QueryErrorCode.INVALID_REQUEST, "QueryResult artifact read request is invalid")
        except Exception:
            return _error(correlation, QueryErrorCode.BINDING_UNAVAILABLE, "QueryResult artifact is unavailable")
        if result.asset_digest != full_digest:
            return _error(correlation, QueryErrorCode.INTERNAL_ERROR, "QueryResult artifact changed from Catalog binding")
        return QueryResult(
            status="ok",
            trace_id=correlation.trace_id,
            data={
                "query_result_id": query_result_id,
                "resource_uri": resource_uri,
                "start": result.start,
                "end": result.end,
                "content_digest": result.content_digest,
                "artifact_digest": full_digest,
                "artifact_format": str(metadata.get("artifact_format") or "application/octet-stream"),
                "content_base64": base64.b64encode(result.content).decode("ascii"),
                "truncated": result.end - result.start >= MAX_BLOB_READ_BYTES,
                "next_locator": {"start": result.end}
                if result.end - result.start >= MAX_BLOB_READ_BYTES else None,
            },
            evidence=(Evidence(
                asset_id=query_result_id,
                resource_uri=resource_uri,
                locator={"chunk_id": f"bytes:{result.start}-{result.end}"},
                revision=full_digest,
                matched_by=("query_result_artifact",),
            ),),
            provenance=Provenance(
                space_id=space_id,
                dataset_id=None,
                dataset_version=None,
                capability="knowledge_read",
                catalog_revision=_valid_digest(self._catalog.catalog_revision),
            ),
        )


class _ProviderQueryService:
    def __init__(
        self,
        *,
        provider: RetrievalProvider,
        catalog: CatalogQueryRepository,
        normalizer: CitationNormalizer | None,
        capability: str,
    ) -> None:
        self._provider = provider
        self._catalog = catalog
        self._normalizer = normalizer or DeterministicCitationNormalizer()
        self._capability = capability

    async def query(
        self,
        *,
        principal: Principal,
        correlation: Correlation,
        query: str,
        space_id: str | None = None,
        limit: int = 20,
    ) -> QueryResult:
        if not _authorized_for_space(principal, space_id, operation="search"):
            return _error(correlation, QueryErrorCode.PERMISSION_DENIED, "knowledge.search scope is required")
        if type(query) is not str or not query.strip():
            return _error(correlation, QueryErrorCode.INVALID_REQUEST, "query must not be empty")
        if len(query) > _MAX_QUERY_LENGTH:
            return _error(correlation, QueryErrorCode.RESOURCE_LIMIT_EXCEEDED, "query is too long")
        if space_id is not None and (type(space_id) is not str or not _OPAQUE_ID_RE.fullmatch(space_id)):
            return _error(correlation, QueryErrorCode.INVALID_REQUEST, "space_id must be an opaque identifier")
        if type(limit) is not int or not 1 <= limit <= _MAX_RETRIEVAL_LIMIT:
            return _error(correlation, QueryErrorCode.RESOURCE_LIMIT_EXCEEDED, "limit is out of range")
        try:
            revision_before = _valid_digest(self._catalog.catalog_revision)
        except Exception:
            revision_before = None
        if revision_before is None:
            return _error(correlation, QueryErrorCode.BINDING_UNAVAILABLE, "Catalog revision is unavailable")
        try:
            candidates = tuple(await self._provider.search(query=query, space_id=space_id, limit=limit))
            if len(candidates) > limit:
                raise ValueError("provider returned more candidates than requested")
            catalog_assets: dict[str, Mapping[str, object]] = {}
            for candidate in candidates:
                if not isinstance(candidate, CitationCandidate):
                    raise ValueError("provider returned a non-contract candidate")
                uri_parts = candidate.resource_uri.removeprefix("knowledge://").split("/")
                if len(uri_parts) != 4 or uri_parts[0] != "spaces" or uri_parts[2] != "assets":
                    raise ValueError("provider returned an invalid Asset URI")
                if candidate.asset_id != uri_parts[3]:
                    raise ValueError("provider returned mismatched Asset identity")
                if space_id is not None and uri_parts[1] != space_id:
                    raise ValueError("provider returned an Asset from another Space")
                asset = self._catalog.get_asset(asset_id=candidate.asset_id)
                if (
                    asset is None
                    or str(asset.get("source_uri") or "") != candidate.resource_uri
                    or str(asset.get("space_id") or "") != uri_parts[1]
                ):
                    raise ValueError("provider returned an Asset not bound to Catalog")
                catalog_assets[candidate.asset_id] = asset
            normalized = self._normalizer.normalize(candidates, max_items=limit)
            if len(normalized) > limit:
                raise ValueError("normalizer returned more evidence than requested")
            bound_evidence: list[Evidence] = []
            for item in normalized:
                uri_parts = item.resource_uri.removeprefix("knowledge://").split("/")
                if len(uri_parts) != 4 or uri_parts[0] != "spaces" or uri_parts[2] != "assets":
                    raise ValueError("normalizer returned an invalid Asset URI")
                if item.asset_id != uri_parts[3]:
                    raise ValueError("normalizer returned mismatched Asset identity")
                if space_id is not None and uri_parts[1] != space_id:
                    raise ValueError("normalizer returned an Asset from another Space")
                asset = catalog_assets.get(item.asset_id)
                if asset is None or item.resource_uri != str(asset.get("source_uri") or ""):
                    raise ValueError("normalizer returned an Asset not bound to Catalog")
                revision = _valid_digest(asset.get("revision")) or _valid_digest(asset.get("content_digest"))
                if revision is None:
                    raise ValueError("Catalog Asset revision is unavailable")
                bound_evidence.append(
                    Evidence(
                        asset_id=item.asset_id,
                        resource_uri=item.resource_uri,
                        locator=item.locator,
                        quote=item.quote,
                        score=item.score,
                        revision=revision,
                        matched_by=item.matched_by,
                    )
                )
            evidence = tuple(bound_evidence)
            revision_after = _valid_digest(self._catalog.catalog_revision)
            if revision_after is None or revision_after != revision_before:
                raise ValueError("Catalog changed during retrieval")
        except Exception as error:
            return _query_error_for_provider(correlation, error)
        return QueryResult(
            status="ok",
            trace_id=correlation.trace_id,
            data={"query": query, "count": len(evidence), "limit": limit},
            evidence=evidence,
            provenance=Provenance(
                space_id=space_id or "catalog",
                dataset_id=None,
                dataset_version=None,
                capability=self._capability,
                catalog_revision=revision_before,
            ),
        )


class DocumentRetrievalService(_ProviderQueryService):
    """Provider-neutral document RAG boundary; fusion/rerank stay behind the port."""

    def __init__(self, provider: RetrievalProvider, catalog: CatalogQueryRepository, normalizer: CitationNormalizer | None = None) -> None:
        super().__init__(provider=provider, catalog=catalog, normalizer=normalizer, capability="document_rag_query")


class WikiQueryService(_ProviderQueryService):
    """Read published Wiki search results; compilation and publication are out of scope."""

    def __init__(self, provider: RetrievalProvider, catalog: CatalogQueryRepository, normalizer: CitationNormalizer | None = None) -> None:
        super().__init__(provider=provider, catalog=catalog, normalizer=normalizer, capability="wiki_query")
