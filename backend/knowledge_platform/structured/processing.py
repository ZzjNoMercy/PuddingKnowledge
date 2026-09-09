"""Processing-plane validation and publication for logical Structured Assets."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from knowledge_contracts import Correlation, Evidence, Principal, Provenance, QueryError, QueryErrorCode, QueryResult

from .ports import StructuredAssetCatalog, StructuredAssetPublisher, StructuredSourceProfiler

_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,160}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_COLUMN_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,200}$")
_SECRET_RE = re.compile(r"(?i)(?:password|secret|token|authorization|api[_ -]?key|private[_ -]?key)")
_MAX_ROWS = 100_000


def _error(correlation: Correlation, code: QueryErrorCode, message: str) -> QueryResult:
    return QueryResult(status="error", trace_id=correlation.trace_id, error=QueryError(code=code, message=message))


def _processing_authorized(principal: Principal, space_id: str) -> bool:
    scopes = set(principal.scopes)
    if principal.tenant_id is not None:
        return False
    has_plane_scope = bool({"knowledge.processing", "knowledge:processing", "knowledge.admin", "knowledge:admin"} & scopes)
    has_space_scope = bool({f"knowledge.space:{space_id}", f"knowledge:space:{space_id}"} & scopes)
    return has_plane_scope and has_space_scope


@dataclass(frozen=True, slots=True)
class LogicalDatasetProcessingRequest:
    dataset_id: str
    space_id: str
    source_paths: Mapping[str, Path]

    def __post_init__(self) -> None:
        if not _ID_RE.fullmatch(self.dataset_id) or not _ID_RE.fullmatch(self.space_id):
            raise ValueError("logical dataset processing identity is invalid")
        if not isinstance(self.source_paths, Mapping) or not self.source_paths:
            raise ValueError("source_paths must be non-empty")
        if any(not _ID_RE.fullmatch(str(source_id)) for source_id in self.source_paths):
            raise ValueError("source_paths contains an invalid source ID")


class LogicalDatasetProcessingService:
    """Validate explicit local source bindings, then publish with a Catalog CAS."""

    def __init__(
        self,
        *,
        catalog: StructuredAssetCatalog,
        profiler: StructuredSourceProfiler,
        publisher: StructuredAssetPublisher,
    ) -> None:
        self._catalog = catalog
        self._profiler = profiler
        self._publisher = publisher

    async def process(
        self,
        *,
        principal: Principal,
        correlation: Correlation,
        request: LogicalDatasetProcessingRequest,
    ) -> QueryResult:
        if not _processing_authorized(principal, request.space_id):
            return _error(correlation, QueryErrorCode.PERMISSION_DENIED, "logical dataset processing requires Processing scope")
        try:
            revision_before = str(self._catalog.catalog_revision)
            if not _DIGEST_RE.fullmatch(revision_before):
                return _error(correlation, QueryErrorCode.BINDING_UNAVAILABLE, "Catalog revision is unavailable")
            dataset = self._catalog.get_structured_asset(asset_id=request.dataset_id)
            logical = dataset.get("logical_dataset") if isinstance(dataset, Mapping) else None
            source_ids = logical.get("source_asset_ids") if isinstance(logical, Mapping) else None
            canonical_columns = logical.get("canonical_columns") if isinstance(logical, Mapping) else None
            if (
                dataset is None
                or str(dataset.get("space_id") or "") != request.space_id
                or str(dataset.get("reference_status") or "") != "pending"
                or not isinstance(logical, Mapping)
                or logical.get("materialization") not in {"virtual", "snapshot"}
                or not isinstance(source_ids, list)
                or not source_ids
                or len(set(source_ids)) != len(source_ids)
                or any(type(item) is not str or not _ID_RE.fullmatch(item) for item in source_ids)
                or not isinstance(canonical_columns, list)
                or not canonical_columns
                or len(set(canonical_columns)) != len(canonical_columns)
                or any(type(item) is not str or not _COLUMN_RE.fullmatch(item) or _SECRET_RE.search(item) for item in canonical_columns)
            ):
                return _error(correlation, QueryErrorCode.BINDING_UNAVAILABLE, "logical dataset is not processable")
            source_ids = [str(item) for item in source_ids]
            canonical_columns = tuple(str(item) for item in canonical_columns)
            if set(request.source_paths) != set(source_ids):
                return _error(correlation, QueryErrorCode.BINDING_UNAVAILABLE, "source file bindings are incomplete")
            source_records = [self._catalog.get_structured_asset(asset_id=source_id) for source_id in source_ids]
            if any(
                source is None
                or str(source.get("space_id") or "") != request.space_id
                or str(source.get("reference_status") or "") not in {"ready", "verified", "active"}
                or "table_query" not in {str(item) for item in (source.get("capabilities") or [])}
                or not _DIGEST_RE.fullmatch(str(source.get("content_digest") or ""))
                for source in source_records
            ):
                return _error(correlation, QueryErrorCode.BINDING_UNAVAILABLE, "logical dataset source is not approved")
            profiles = []
            for source_id, source in zip(source_ids, source_records, strict=True):
                profile = self._profiler.inspect_source(
                    path=request.source_paths[source_id],
                    sheet_name=source.get("sheet_name") if isinstance(source, Mapping) else None,
                )
                if profile.columns != canonical_columns or profile.content_digest != str(source["content_digest"]):
                    return _error(correlation, QueryErrorCode.BINDING_UNAVAILABLE, "source content or schema changed")
                profiles.append((source_id, profile))
            row_count = sum(profile.row_count for _, profile in profiles)
            if row_count > _MAX_ROWS:
                return _error(correlation, QueryErrorCode.RESOURCE_LIMIT_EXCEEDED, "logical dataset exceeds row limit")
            from .local import LocalStructuredFileProvider

            content_digest = LocalStructuredFileProvider.logical_content_digest(
                [(source_id, profile.content_digest) for source_id, profile in profiles]
            )
            source_snapshot = tuple(
                {
                    "asset_id": source_id,
                    "content_digest": profile.content_digest,
                    "row_count": profile.row_count,
                }
                for source_id, profile in profiles
            )
            written = self._publisher.publish_logical_dataset(
                principal=principal,
                dataset_id=request.dataset_id,
                expected_definition_digest=str(dataset.get("content_digest") or ""),
                content_digest=content_digest,
                columns=canonical_columns,
                row_count=row_count,
                source_snapshot=source_snapshot,
            )
            if not isinstance(written, Mapping) or str(written.get("reference_status") or "") != "ready":
                raise ValueError("publisher returned a non-ready record")
            catalog_record = self._catalog.get_structured_asset(asset_id=request.dataset_id)
            if (
                catalog_record is None
                or str(catalog_record.get("reference_status") or "") not in {"ready", "verified", "active"}
                or str(catalog_record.get("content_digest") or "") != content_digest
            ):
                raise ValueError("Catalog did not confirm the published dataset")
            revision_after = str(self._catalog.catalog_revision)
            if not _DIGEST_RE.fullmatch(revision_after):
                raise ValueError("Catalog revision after publish is unavailable")
        except Exception:
            return _error(correlation, QueryErrorCode.CAPABILITY_UNAVAILABLE, "logical dataset processing is unavailable")
        return QueryResult(
            status="ok",
            trace_id=correlation.trace_id,
            answer="逻辑数据集已完成 source 校验并发布为 ready。",
            data={"dataset": dict(written), "source_snapshot": list(source_snapshot)},
            evidence=(
                Evidence(
                    asset_id=request.dataset_id,
                    resource_uri=str(written.get("source_uri") or ""),
                    locator={"section": "logical_dataset_publication"},
                    quote=str(written.get("title") or "")[:1200],
                    revision=content_digest,
                    matched_by=("processing", "source_digest", "schema"),
                ),
            ),
            provenance=Provenance(
                space_id=request.space_id,
                dataset_id=request.dataset_id,
                dataset_version=None,
                capability="table_query",
                catalog_revision=revision_after,
            ),
        )
