"""Explicit local source binding for pending Structured Assets."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from knowledge_contracts import Correlation, Evidence, Principal, Provenance, QueryError, QueryErrorCode, QueryResult

from .local import LocalStructuredFileProvider
from .ports import (
    StructuredAssetBindingWriter,
    StructuredAssetCatalog,
    StructuredFileBindingVerifier,
    StructuredSourceProfile,
)

_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,160}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _error(correlation: Correlation, code: QueryErrorCode, message: str) -> QueryResult:
    return QueryResult(status="error", trace_id=correlation.trace_id, error=QueryError(code=code, message=message))


def _authorized(principal: Principal, space_id: str) -> bool:
    scopes = set(principal.scopes)
    return (
        principal.tenant_id is None
        and bool({"knowledge.processing", "knowledge:processing", "knowledge.admin", "knowledge:admin"} & scopes)
        and bool({f"knowledge.space:{space_id}", f"knowledge:space:{space_id}"} & scopes)
    )


@dataclass(frozen=True, slots=True)
class StructuredAssetBindingRequest:
    asset_id: str
    space_id: str
    path: Path

    def __post_init__(self) -> None:
        if not _ID_RE.fullmatch(self.asset_id) or not _ID_RE.fullmatch(self.space_id):
            raise ValueError("Structured Asset binding identity is invalid")
        if not isinstance(self.path, Path) or not self.path.is_absolute():
            raise ValueError("Structured Asset binding path must be absolute")


class LocalStructuredFileBindingVerifier(StructuredFileBindingVerifier):
    """Verify bytes only; it does not copy or expose the physical path."""

    def verify_file(
        self, *, path: Path, expected_digest: str, expected_size_bytes: int | None
    ) -> StructuredSourceProfile:
        if not _DIGEST_RE.fullmatch(expected_digest):
            raise ValueError("expected source digest is invalid")
        if expected_size_bytes is not None and (type(expected_size_bytes) is not int or expected_size_bytes < 0):
            raise ValueError("expected source size is invalid")
        digest = LocalStructuredFileProvider.file_digest(path)
        if digest != expected_digest:
            raise ValueError("source bytes do not match Catalog digest")
        if expected_size_bytes is not None and path.stat().st_size != expected_size_bytes:
            raise ValueError("source size does not match Catalog")
        return StructuredSourceProfile(columns=(), row_count=0, content_digest=digest)


class StructuredAssetBindingService:
    """CAS-approve one explicitly supplied local source without path persistence."""

    def __init__(
        self,
        *,
        catalog: StructuredAssetCatalog,
        verifier: StructuredFileBindingVerifier,
        writer: StructuredAssetBindingWriter,
    ) -> None:
        self._catalog = catalog
        self._verifier = verifier
        self._writer = writer

    def bind(
        self,
        *,
        principal: Principal,
        correlation: Correlation,
        request: StructuredAssetBindingRequest,
    ) -> QueryResult:
        if not _authorized(principal, request.space_id):
            return _error(correlation, QueryErrorCode.PERMISSION_DENIED, "Structured Asset binding requires Processing scope")
        try:
            revision_before = str(self._catalog.catalog_revision)
            if not _DIGEST_RE.fullmatch(revision_before):
                return _error(correlation, QueryErrorCode.BINDING_UNAVAILABLE, "Catalog revision is unavailable")
            asset = self._catalog.get_structured_asset(asset_id=request.asset_id)
            expected_digest = str(asset.get("content_digest") or "") if isinstance(asset, Mapping) else ""
            expected_size = asset.get("size_bytes") if isinstance(asset, Mapping) else None
            if (
                asset is None
                or str(asset.get("space_id") or "") != request.space_id
                or str(asset.get("reference_status") or "") != "pending"
                or str(asset.get("source_type") or "") == "logical_concat"
                or not _DIGEST_RE.fullmatch(expected_digest)
                or (expected_size is not None and type(expected_size) is not int)
            ):
                return _error(correlation, QueryErrorCode.BINDING_UNAVAILABLE, "Structured Asset is not bindable")
            verified = self._verifier.verify_file(
                path=request.path,
                expected_digest=expected_digest,
                expected_size_bytes=expected_size,
            )
            columns = asset.get("columns")
            row_count = asset.get("row_count")
            if not isinstance(columns, list) or any(type(item) is not str for item in columns):
                return _error(correlation, QueryErrorCode.BINDING_UNAVAILABLE, "Structured Asset profile is invalid")
            if type(row_count) is not int or row_count < 0:
                return _error(correlation, QueryErrorCode.BINDING_UNAVAILABLE, "Structured Asset row profile is unavailable")
            profile = StructuredSourceProfile(
                columns=tuple(columns),
                row_count=row_count,
                content_digest=verified.content_digest,
            )
            written = self._writer.bind_source_asset(
                principal=principal,
                asset_id=request.asset_id,
                space_id=request.space_id,
                expected_content_digest=expected_digest,
                profile=profile,
            )
            if not isinstance(written, Mapping) or str(written.get("reference_status") or "") not in {"ready", "verified", "active"}:
                raise ValueError("binding writer did not return an approved Asset")
            current = self._catalog.get_structured_asset(asset_id=request.asset_id)
            if (
                current is None
                or str(current.get("space_id") or "") != request.space_id
                or str(current.get("reference_status") or "") not in {"ready", "verified", "active"}
                or str(current.get("content_digest") or "") != expected_digest
            ):
                raise ValueError("Catalog did not confirm source binding")
            revision_after = str(self._catalog.catalog_revision)
            if not _DIGEST_RE.fullmatch(revision_after) or revision_after == revision_before:
                raise ValueError("Catalog revision did not advance after source binding")
        except FileNotFoundError:
            return _error(correlation, QueryErrorCode.BINDING_UNAVAILABLE, "Structured source is unavailable")
        except (OSError, TypeError, ValueError):
            return _error(correlation, QueryErrorCode.BINDING_UNAVAILABLE, "Structured source binding failed")
        return QueryResult(
            status="ok",
            trace_id=correlation.trace_id,
            answer="Structured Asset source binding is ready for Table Query.",
            data={"asset": dict(written), "content_digest": expected_digest},
            evidence=(
                Evidence(
                    asset_id=request.asset_id,
                    resource_uri=str(written.get("source_uri") or ""),
                    locator={"section": "structured_source_binding"},
                    quote=str(written.get("title") or "")[:1200],
                    revision=expected_digest,
                    matched_by=("source_digest", "explicit_path_binding"),
                ),
            ),
            provenance=Provenance(
                space_id=request.space_id,
                dataset_id=request.asset_id,
                dataset_version=None,
                capability="table_query",
                catalog_revision=revision_after,
            ),
        )
