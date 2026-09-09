"""Admin-plane enqueue contract for semantic-dimension Processing jobs."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol

from knowledge_contracts import (
    Correlation,
    Evidence,
    Principal,
    Provenance,
    QueryError,
    QueryErrorCode,
    QueryResult,
)
from knowledge_platform.structured.ports import StructuredAssetCatalog

_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,160}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SECRET_RE = re.compile(r"(?i)(?:password|secret|token|authorization|api[_ -]?key|private[_ -]?key)")
_PATH_RE = re.compile(r"(?:^|[/\\])(?:Users|home|tmp|private|var|etc|opt|usr|root)(?:[/\\]|$)|\.\.(?:[/\\]|$)")
_MAX_JSON_BYTES = 128 * 1024
_LEGACY_ID_KEYS = frozenset({"session_id", "query_id", "run_id", "goal_id"})


def _error(correlation: Correlation, code: QueryErrorCode, message: str) -> QueryResult:
    return QueryResult(status="error", trace_id=correlation.trace_id, error=QueryError(code=code, message=message))


def _unsafe(value: object) -> bool:
    if isinstance(value, str):
        return bool(_SECRET_RE.search(value) or _PATH_RE.search(value))
    if isinstance(value, Mapping):
        return any(
            str(key).casefold() in _LEGACY_ID_KEYS or _unsafe(key) or _unsafe(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_unsafe(item) for item in value)
    return False


def _admin_authorized(principal: Principal, space_id: str) -> bool:
    scopes = set(principal.scopes)
    return (
        principal.tenant_id is None
        and bool({"knowledge.admin", "knowledge:admin", "knowledge.semantic_authoring", "knowledge:semantic_authoring"} & scopes)
        and bool({f"knowledge.space:{space_id}", f"knowledge:space:{space_id}"} & scopes)
    )


@dataclass(frozen=True, slots=True)
class SemanticDimensionAuthoringRequest:
    dimension_id: str
    space_id: str
    adapter: str
    requested_scope: Mapping[str, object] = field(default_factory=dict)
    input_snapshot: Mapping[str, object] = field(default_factory=dict)
    title: str = ""

    def __post_init__(self) -> None:
        if not _ID_RE.fullmatch(self.dimension_id) or not _ID_RE.fullmatch(self.space_id) or not _ID_RE.fullmatch(self.adapter):
            raise ValueError("semantic dimension identity is invalid")
        if self.title and (len(self.title) > 500 or _SECRET_RE.search(self.title) or _PATH_RE.search(self.title)):
            raise ValueError("semantic dimension title is unsafe")
        for field_name in ("requested_scope", "input_snapshot"):
            value = getattr(self, field_name)
            if not isinstance(value, Mapping) or _unsafe(value):
                raise ValueError(f"{field_name} is unsafe")
            try:
                if len(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")) > _MAX_JSON_BYTES:
                    raise ValueError(f"{field_name} is too large")
            except (TypeError, ValueError) as error:
                raise ValueError(f"{field_name} is not JSON-safe") from error

    def fingerprint(self, *, source_snapshot: tuple[Mapping[str, object], ...]) -> str:
        material = {
            "dimension_id": self.dimension_id,
            "space_id": self.space_id,
            "adapter": self.adapter,
            "requested_scope": dict(self.requested_scope),
            "input_snapshot": dict(self.input_snapshot),
            "source_snapshot": [dict(item) for item in source_snapshot],
        }
        encoded = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


class SemanticDimensionJobWriter(Protocol):
    def create_authoring_job(self, *, record: Mapping[str, object]) -> Mapping[str, object]: ...

    def decide_authoring_job(
        self,
        *,
        job_id: str,
        space_id: str,
        decision: str,
        expected_status: str,
        actor_digest: str,
        correlation_digest: str,
    ) -> Mapping[str, object]: ...


@dataclass(frozen=True, slots=True)
class SemanticDimensionJobDecisionRequest:
    """An explicit Platform decision for a job paused at a business boundary."""

    job_id: str
    space_id: str
    decision: str
    expected_status: str

    def __post_init__(self) -> None:
        if not _ID_RE.fullmatch(self.job_id) or not _ID_RE.fullmatch(self.space_id):
            raise ValueError("semantic job decision identity is invalid")
        if self.decision not in {"confirm", "reject"}:
            raise ValueError("semantic job decision must be confirm or reject")
        if self.expected_status not in {
            "waiting_for_publish_confirmation",
            "waiting_for_baseline_change_confirmation",
        }:
            raise ValueError("semantic job decision expected_status is invalid")


class SemanticDimensionJobDecisionService:
    """Resolve a waiting job through an auditable Platform Admin command."""

    def __init__(self, *, writer: SemanticDimensionJobWriter) -> None:
        self._writer = writer

    def decide(
        self,
        *,
        principal: Principal,
        correlation: Correlation,
        request: SemanticDimensionJobDecisionRequest,
    ) -> QueryResult:
        if not _admin_authorized(principal, request.space_id):
            return _error(correlation, QueryErrorCode.PERMISSION_DENIED, "semantic job decisions require Admin scope")
        actor_digest = "sha256:" + hashlib.sha256(principal.subject_id.encode("utf-8")).hexdigest()
        correlation_digest = "sha256:" + hashlib.sha256(correlation.trace_id.encode("utf-8")).hexdigest()
        try:
            written = self._writer.decide_authoring_job(
                job_id=request.job_id,
                space_id=request.space_id,
                decision=request.decision,
                expected_status=request.expected_status,
                actor_digest=actor_digest,
                correlation_digest=correlation_digest,
            )
            if not isinstance(written, Mapping) or str(written.get("id") or "") != request.job_id:
                raise ValueError("job decision writer returned an invalid record")
        except PermissionError:
            return _error(correlation, QueryErrorCode.PERMISSION_DENIED, "semantic job decision is not authorized")
        except (LookupError, ValueError):
            return _error(correlation, QueryErrorCode.INVALID_REQUEST, "semantic job decision conflicts with current job state")
        except Exception:
            return _error(correlation, QueryErrorCode.CAPABILITY_UNAVAILABLE, "semantic job decision is unavailable")
        return QueryResult(
            status="ok",
            trace_id=correlation.trace_id,
            answer="语义维度任务已确认并重新入队。" if request.decision == "confirm" else "语义维度任务已拒绝并取消。",
            data={"job": dict(written), "decision": request.decision},
            evidence=(
                Evidence(
                    asset_id=request.job_id,
                    resource_uri=str(written.get("scope_uri") or ""),
                    locator={"section": "semantic_job_decision"},
                    quote=request.decision,
                    revision=correlation_digest,
                    matched_by=("admin_decision", request.expected_status),
                ),
            ),
            provenance=Provenance(
                space_id=request.space_id,
                dataset_id=None,
                dataset_version=None,
                capability="semantic_authoring",
                catalog_revision=None,
            ),
        )


class SemanticDimensionAuthoringService:
    """Create a queued Platform job; worker execution and publishing are separate."""

    def __init__(self, *, catalog: StructuredAssetCatalog, writer: SemanticDimensionJobWriter) -> None:
        self._catalog = catalog
        self._writer = writer

    def enqueue(
        self,
        *,
        principal: Principal,
        correlation: Correlation,
        request: SemanticDimensionAuthoringRequest,
    ) -> QueryResult:
        if not _admin_authorized(principal, request.space_id):
            return _error(correlation, QueryErrorCode.PERMISSION_DENIED, "semantic authoring requires Admin scope")
        try:
            revision_before = str(self._catalog.catalog_revision)
            if not _DIGEST_RE.fullmatch(revision_before):
                return _error(correlation, QueryErrorCode.BINDING_UNAVAILABLE, "Catalog revision is unavailable")
            raw_source_ids = request.input_snapshot.get("source_asset_ids", ())
            if not isinstance(raw_source_ids, (list, tuple)) or not raw_source_ids:
                return _error(correlation, QueryErrorCode.BINDING_UNAVAILABLE, "semantic dimension sources are required")
            source_ids = tuple(str(item) for item in raw_source_ids)
            if len(set(source_ids)) != len(source_ids) or any(not _ID_RE.fullmatch(item) for item in source_ids):
                return _error(correlation, QueryErrorCode.INVALID_REQUEST, "semantic dimension source IDs are invalid")
            sources = tuple(self._catalog.get_structured_asset(asset_id=item) for item in source_ids)
            if any(
                source is None
                or str(source.get("space_id") or "") != request.space_id
                or str(source.get("reference_status") or "") not in {"ready", "verified", "active"}
                or "table_query" not in {str(item) for item in (source.get("capabilities") or [])}
                or not _DIGEST_RE.fullmatch(str(source.get("content_digest") or ""))
                for source in sources
            ):
                return _error(correlation, QueryErrorCode.BINDING_UNAVAILABLE, "semantic dimension source is not approved")
            source_snapshot = tuple(
                {"asset_id": item, "content_digest": str(source["content_digest"])}
                for item, source in zip(source_ids, sources, strict=True)
            )
            job_id = "authoring_" + request.fingerprint(source_snapshot=source_snapshot).removeprefix("sha256:")[:48]
            scope_uri = f"knowledge://spaces/{request.space_id}/semantic-dimensions/{request.dimension_id}"
            record = {
                "id": job_id,
                "kind": "semantic_dimension_build",
                "dimension_id": request.dimension_id,
                "adapter": request.adapter,
                "scope_uri": scope_uri,
                "scope_json": dict(request.requested_scope),
                "input_snapshot_json": {
                    **dict(request.input_snapshot),
                    "source_asset_ids": list(source_ids),
                    "source_snapshot": list(source_snapshot),
                },
                "status": "queued",
                "current_step": "queued",
                "progress": 0,
                "staging_uri": "",
                "staging_reference_digest": "",
                "published_uri": "",
                "published_reference_digest": "",
                "result_summary_json": {},
                "correlation_json": {"trace_id": correlation.trace_id},
                "error_message": "",
                "retry_count": 0,
                "metadata_json": {"title": request.title} if request.title else {},
                "source_snapshot": source_snapshot,
                "principal": principal,
            }
            written = self._writer.create_authoring_job(record=record)
            if not isinstance(written, Mapping) or str(written.get("id") or "") != job_id or str(written.get("status") or "") != "queued":
                raise ValueError("authoring job writer returned an invalid record")
            current_sources = tuple(self._catalog.get_structured_asset(asset_id=item) for item in source_ids)
            if any(
                source is None
                or str(source.get("space_id") or "") != request.space_id
                or str(source.get("reference_status") or "") not in {"ready", "verified", "active"}
                or str(source.get("content_digest") or "") != snapshot["content_digest"]
                for source, snapshot in zip(current_sources, source_snapshot, strict=True)
            ):
                raise ValueError("semantic dimension sources changed during authoring")
            revision_after = str(self._catalog.catalog_revision)
            if not _DIGEST_RE.fullmatch(revision_after):
                raise ValueError("Catalog revision after authoring is unavailable")
        except Exception:
            return _error(correlation, QueryErrorCode.CAPABILITY_UNAVAILABLE, "semantic authoring is unavailable")
        return QueryResult(
            status="ok",
            trace_id=correlation.trace_id,
            answer="语义维度 Processing 任务已入队，等待 worker 处理。",
            data={"job": dict(written), "source_snapshot": list(source_snapshot)},
            evidence=(
                Evidence(
                    asset_id=job_id,
                    resource_uri=scope_uri,
                    locator={"section": "semantic_dimension_job"},
                    quote=request.dimension_id,
                    revision=request.fingerprint(source_snapshot=source_snapshot),
                    matched_by=("admin_authoring", "source_binding"),
                ),
            ),
            provenance=Provenance(
                space_id=request.space_id,
                dataset_id=None,
                dataset_version=None,
                capability="semantic_authoring",
                catalog_revision=revision_after,
            ),
        )
