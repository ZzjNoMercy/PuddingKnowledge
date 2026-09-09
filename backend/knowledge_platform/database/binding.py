"""Admin-plane binding of a validated Database Dataset to a Collection."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from knowledge_contracts import Correlation, Evidence, Principal, Provenance, QueryError, QueryErrorCode, QueryResult

from .ports import DatabaseDatasetResolver

_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
_ADMIN_SCOPES = {"knowledge.admin", "knowledge:admin", "knowledge.processing", "knowledge:processing"}


@dataclass(frozen=True, slots=True)
class DatabaseCollectionBindingRequest:
    """Portable IDs only; connection details remain in the host resolver."""

    collection_id: str
    collection_version: str
    space_id: str
    dataset_id: str

    def __post_init__(self) -> None:
        for value, field_name in (
            (self.collection_id, "collection_id"),
            (self.collection_version, "collection_version"),
            (self.space_id, "space_id"),
            (self.dataset_id, "dataset_id"),
        ):
            if not isinstance(value, str) or not _ID_RE.fullmatch(value):
                raise ValueError(f"database Collection binding {field_name} is invalid")


class DatabaseCollectionBindingWriter(Protocol):
    def bind_collection_provider(
        self,
        *,
        principal: Principal,
        collection_id: str,
        collection_version: str,
        space_id: str,
        capability: str,
        binding: Mapping[str, str],
    ) -> Mapping[str, object]: ...


def _error(correlation: Correlation, code: QueryErrorCode, message: str) -> QueryResult:
    return QueryResult(status="error", trace_id=correlation.trace_id, error=QueryError(code=code, message=message))


class DatabaseCollectionBindingService:
    """Validate a current host source, then persist one explicit Collection binding."""

    def __init__(self, *, datasets: DatabaseDatasetResolver, writer: DatabaseCollectionBindingWriter) -> None:
        self._datasets = datasets
        self._writer = writer

    def bind(
        self,
        *,
        principal: Principal,
        correlation: Correlation,
        request: DatabaseCollectionBindingRequest,
    ) -> QueryResult:
        scopes = set(principal.scopes)
        if principal.tenant_id is not None or not (_ADMIN_SCOPES & scopes):
            return _error(correlation, QueryErrorCode.PERMISSION_DENIED, "database Collection binding requires Admin/Processing scope")
        if not ({f"knowledge.space:{request.space_id}", f"knowledge:space:{request.space_id}"} & scopes):
            return _error(correlation, QueryErrorCode.PERMISSION_DENIED, "database Collection binding Space scope is required")
        try:
            binding = self._datasets.resolve(dataset_id=request.dataset_id, space_id=request.space_id)
        except Exception:
            return _error(correlation, QueryErrorCode.BINDING_UNAVAILABLE, "database Dataset source is unavailable")
        if binding is None or binding.space_id != request.space_id or binding.dataset_id != request.dataset_id:
            return _error(correlation, QueryErrorCode.BINDING_UNAVAILABLE, "database Dataset source is unavailable")
        try:
            self._writer.bind_collection_provider(
                principal=principal,
                collection_id=request.collection_id,
                collection_version=request.collection_version,
                space_id=request.space_id,
                capability="database_nl2sql",
                binding={"dataset_id": request.dataset_id},
            )
        except PermissionError:
            return _error(correlation, QueryErrorCode.PERMISSION_DENIED, "database Collection binding is not authorized")
        except (TypeError, ValueError):
            return _error(correlation, QueryErrorCode.BINDING_UNAVAILABLE, "database Collection binding could not be persisted")
        return QueryResult(
            status="ok",
            trace_id=correlation.trace_id,
            answer="数据库 Dataset 已绑定到 Collection。",
            data={
                "binding": {
                    "collection_id": request.collection_id,
                    "collection_version": request.collection_version,
                    "space_id": request.space_id,
                    "dataset_id": request.dataset_id,
                    "provider": "database_nl2sql",
                }
            },
            evidence=(
                Evidence(
                    asset_id=request.dataset_id,
                    resource_uri=f"knowledge://spaces/{request.space_id}/databases/{request.dataset_id}",
                    locator={"section": "collection_database_binding"},
                    quote=binding.source_revision,
                    revision=binding.source_revision,
                    matched_by=("explicit_database_source", "admin_collection_binding"),
                ),
            ),
            provenance=Provenance(
                space_id=request.space_id,
                dataset_id=request.dataset_id,
                dataset_version=binding.dataset_version,
                capability="database_nl2sql",
                provider_versions={"database": binding.provider_version},
            ),
        )
