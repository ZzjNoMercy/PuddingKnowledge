"""Admin-plane authoring contract for logical Structured Assets."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from knowledge_contracts import Correlation, Evidence, Principal, Provenance, QueryError, QueryErrorCode, QueryResult

from .ports import StructuredAssetCatalog

_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,160}$")
_COLUMN_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,200}$")
_SECRET_RE = re.compile(r"(?i)(?:password|secret|token|authorization|api[_ -]?key|private[_ -]?key)")
_PATH_RE = re.compile(r"(?:^|[/\\])(?:Users|home|tmp|private|var|etc)(?:[/\\]|$)|\.\.(?:[/\\]|$)")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _error(correlation: Correlation, code: QueryErrorCode, message: str) -> QueryResult:
    return QueryResult(status="error", trace_id=correlation.trace_id, error=QueryError(code=code, message=message))


def _admin_authorized(principal: Principal, space_id: str) -> bool:
    scopes = set(principal.scopes)
    if principal.tenant_id is not None:
        return False
    if not {"knowledge.admin", "knowledge:admin"} & scopes:
        return False
    return bool(
        {
            "knowledge.admin",
            "knowledge:admin",
            f"knowledge.space:{space_id}",
            f"knowledge:space:{space_id}",
        }
        & scopes
    )


@dataclass(frozen=True, slots=True)
class LogicalDatasetAuthoringRequest:
    """Portable logical-union definition accepted by the Admin plane."""

    dataset_id: str
    space_id: str
    title: str
    source_asset_ids: tuple[str, ...]
    canonical_columns: tuple[str, ...]
    description: str = ""
    schema_mode: str = "strict"
    tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field_name in ("dataset_id", "space_id"):
            if not _ID_RE.fullmatch(getattr(self, field_name)):
                raise ValueError(f"{field_name} is invalid")
        if not isinstance(self.title, str) or not self.title.strip() or len(self.title) > 500:
            raise ValueError("title is invalid")
        if self.schema_mode not in {"strict"}:
            raise ValueError("schema_mode is invalid")
        if not self.source_asset_ids or len(set(self.source_asset_ids)) != len(self.source_asset_ids):
            raise ValueError("source_asset_ids must be non-empty and unique")
        if any(not _ID_RE.fullmatch(item) for item in self.source_asset_ids):
            raise ValueError("source_asset_ids contain an invalid ID")
        if not self.canonical_columns or len(set(self.canonical_columns)) != len(self.canonical_columns):
            raise ValueError("canonical_columns must be non-empty and unique")
        if any(not _COLUMN_RE.fullmatch(item) or _SECRET_RE.search(item) for item in self.canonical_columns):
            raise ValueError("canonical_columns contain an unsafe field")
        if _SECRET_RE.search(self.title) or _PATH_RE.search(self.description):
            raise ValueError("authoring metadata contains unsafe text")
        if any(not isinstance(tag, str) or len(tag) > 100 or _SECRET_RE.search(tag) for tag in self.tags):
            raise ValueError("tags contain unsafe text")

    def definition(self) -> dict[str, object]:
        logical = {
            "formatter": "logical-data-asset",
            "version": "1.0.0",
            "kind": "vertical_union",
            "description": self.description.strip(),
            "tags": list(self.tags),
            "materialization": "virtual",
            "schema_mode": self.schema_mode,
            "source_asset_ids": list(self.source_asset_ids),
            "canonical_columns": list(self.canonical_columns),
            "schema": {"fields": list(self.canonical_columns)},
        }
        return {"logical_dataset": logical}

    def definition_digest(self) -> str:
        encoded = json.dumps(self.definition(), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


class StructuredAssetWriter(Protocol):
    """Persistence port owned by the Catalog/Admin plane."""

    def create_logical_dataset(self, *, record: Mapping[str, object]) -> Mapping[str, object]: ...


class LogicalDatasetAuthoringService:
    """Validate and stage a logical dataset without running a data job."""

    def __init__(self, *, catalog: StructuredAssetCatalog, writer: StructuredAssetWriter) -> None:
        self._catalog = catalog
        self._writer = writer

    def create(
        self,
        *,
        principal: Principal,
        correlation: Correlation,
        request: LogicalDatasetAuthoringRequest,
    ) -> QueryResult:
        if not _admin_authorized(principal, request.space_id):
            return _error(correlation, QueryErrorCode.PERMISSION_DENIED, "logical dataset authoring requires Admin scope")
        try:
            revision_before = str(self._catalog.catalog_revision)
            if not _DIGEST_RE.fullmatch(revision_before):
                return _error(correlation, QueryErrorCode.BINDING_UNAVAILABLE, "Catalog revision is unavailable")
            source_records = [
                self._catalog.get_structured_asset(asset_id=asset_id) for asset_id in request.source_asset_ids
            ]
            if any(
                source is None
                or str(source.get("space_id") or "") != request.space_id
                or str(source.get("reference_status") or "") not in {"ready", "verified", "active"}
                or "table_query" not in {str(item) for item in (source.get("capabilities") or [])}
                for source in source_records
            ):
                return _error(correlation, QueryErrorCode.BINDING_UNAVAILABLE, "logical dataset source is not approved")
            definition = request.definition()
            definition_digest = request.definition_digest()
            source_snapshot = tuple(
                {
                    "asset_id": str(asset_id),
                    "content_digest": str(source["content_digest"]),
                }
                for asset_id, source in zip(request.source_asset_ids, source_records, strict=True)
            )
            record = {
                "id": request.dataset_id,
                "space_id": request.space_id,
                "kind": "structured_asset",
                "source_type": "logical_concat",
                "title": request.title.strip(),
                "source_uri": f"knowledge://spaces/{request.space_id}/structured-assets/{request.dataset_id}/source",
                "content_digest": definition_digest,
                "reference_status": "pending",
                "capabilities": ["table_query"],
                "source_snapshot": source_snapshot,
                **definition,
            }
            written = self._writer.create_logical_dataset(record=record)
            if not isinstance(written, Mapping):
                raise ValueError("writer returned an invalid record")
            if (
                str(written.get("id") or "") != request.dataset_id
                or str(written.get("space_id") or "") != request.space_id
                or str(written.get("source_uri") or "") != record["source_uri"]
                or str(written.get("content_digest") or "") != definition_digest
                or str(written.get("reference_status") or "") != "pending"
            ):
                raise ValueError("writer returned a record outside the authoring contract")
            current_sources = [
                self._catalog.get_structured_asset(asset_id=asset_id) for asset_id in request.source_asset_ids
            ]
            if any(
                current is None
                or str(current.get("space_id") or "") != request.space_id
                or str(current.get("reference_status") or "") not in {"ready", "verified", "active"}
                or str(current.get("content_digest") or "") != str(before.get("content_digest") or "")
                for current, before in zip(current_sources, source_records, strict=True)
            ):
                raise ValueError("logical dataset sources changed during authoring")
            revision_after = str(self._catalog.catalog_revision)
            if not _DIGEST_RE.fullmatch(revision_after):
                raise ValueError("Catalog revision after write is unavailable")
        except Exception:
            return _error(correlation, QueryErrorCode.CAPABILITY_UNAVAILABLE, "logical dataset authoring is unavailable")
        return QueryResult(
            status="ok",
            trace_id=correlation.trace_id,
            answer="逻辑数据集已进入 pending，等待 Processing 校验与发布。",
            data={"dataset": dict(written), "definition_digest": definition_digest},
            evidence=(
                Evidence(
                    asset_id=request.dataset_id,
                    resource_uri=str(record["source_uri"]),
                    locator={"section": "logical_dataset_definition"},
                    quote=request.title[:1200],
                    revision=definition_digest,
                    matched_by=("admin_authoring", "source_binding"),
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
