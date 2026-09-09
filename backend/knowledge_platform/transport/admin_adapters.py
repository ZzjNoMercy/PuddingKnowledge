"""REST-shaped Admin/Processing adapters for logical Structured Assets."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from knowledge_contracts import Correlation, Evidence, Principal, Provenance, QueryError, QueryErrorCode, QueryResult
from knowledge_platform.capture import CaptureProcessingError, CaptureProcessingRequest, CaptureProcessingWorker
from knowledge_platform.catalog.connector_authorization import (
    ConnectorAuthorizationRequest,
    ConnectorAuthorizationService,
)
from knowledge_platform.catalog.index_rebuild import CatalogIndexRebuildService, IndexRebuildRequest
from knowledge_platform.catalog.local_asset_binding_review_queue import LocalAssetBindingReviewQueue
from knowledge_platform.catalog.notification_service import NotificationEventQueryService
from knowledge_platform.connector_sync import ConnectorSyncError, ConnectorSyncRequest, ConnectorSyncWorker
from knowledge_platform.database import (
    DatabaseCollectionBindingRequest,
    DatabaseCollectionBindingService,
    SqlGuardrailAdminService,
    SqlGuardrailRule,
)
from knowledge_platform.gbrain import GbrainProjectionRequest, GbrainProjectionService
from knowledge_platform.ingestion import (
    AssetUploadRequest,
    LocalAssetUploadService,
    LocalPackageImportService,
    PackageImportRequest,
)
from knowledge_platform.semantic import (
    SemanticDimensionAuthoringRequest,
    SemanticDimensionAuthoringService,
    SemanticDimensionBuildError,
    SemanticDimensionBuildWorker,
    SemanticDimensionJobDecisionRequest,
    SemanticDimensionJobDecisionService,
    SemanticMarkdownAdminService,
    SemanticMarkdownDefinition,
)
from knowledge_platform.structured import (
    LogicalDatasetAuthoringRequest,
    LogicalDatasetAuthoringService,
    LogicalDatasetProcessingError,
    LogicalDatasetProcessingJobRequest,
    LogicalDatasetProcessingRequest,
    LogicalDatasetProcessingService,
    LogicalDatasetProcessingWorker,
)
from knowledge_platform.wiki import WikiCompilationError, WikiCompilationRequest, WikiCompilationWorker

_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,160}$")
_PUBLISH_PATH_RE = re.compile(r"^/v1/datasets/([A-Za-z0-9._-]{1,160}):publish$")
_WIKI_COMPILE_PATH_RE = re.compile(r"^/v1/wiki/assets/([A-Za-z0-9._:-]{1,160}):compile$")
_SEMANTIC_DECISION_PATH_RE = re.compile(r"^/v1/semantic-dimensions/jobs/([A-Za-z0-9._-]{1,160}):decision$")
_SEMANTIC_PROCESS_PATH_RE = re.compile(r"^/v1/semantic-dimensions/jobs/([A-Za-z0-9._-]{1,160}):process$")
_GUARDRAIL_DECISION_PATH_RE = re.compile(r"^/v1/database/guardrails/([A-Za-z0-9._-]{1,160}):decision$")
_SEMANTIC_MARKDOWN_DECISION_PATH_RE = re.compile(r"^/v1/semantic-assets/([A-Za-z0-9._:-]{1,160}):decision$")
_DATABASE_BINDING_FIELDS = frozenset({"collection_id", "collection_version", "space_id", "dataset_id"})
_WIKI_COMPILE_FIELDS = frozenset(
    {"snapshot_id", "source_revision", "source_uri", "content_digest", "idempotency_key"}
)
_CAPTURE_PROCESS_PATH_RE = re.compile(r"^/v1/captures/assets/([A-Za-z0-9._:-]{1,160}):process$")
_CAPTURE_PROCESS_FIELDS = frozenset({"source_revision", "source_uri", "content_digest", "idempotency_key"})
_CONNECTOR_SYNC_PATH_RE = re.compile(r"^/v1/sources/([A-Za-z0-9._:-]{1,160}):sync$")
_CONNECTOR_SYNC_FIELDS = frozenset({"space_id", "source_item_id", "content_digest", "idempotency_key"})
_CONNECTOR_AUTH_PATH_RE = re.compile(r"^/v1/connectors/([A-Za-z0-9._-]{1,160}):authorize$")
_CONNECTOR_AUTH_FIELDS = frozenset({"space_id", "mode", "idempotency_key"})
_GBRAIN_PROJECTION_PATH_RE = re.compile(r"^/v1/wiki/assets/([A-Za-z0-9._:-]{1,160}):project-gbrain$")
_GBRAIN_PROJECTION_FIELDS = frozenset({"space_id", "source_revision", "published_digest", "idempotency_key", "schema_pack"})
_FRESHNESS_FIELDS = frozenset(
    {
        "collection_id",
        "collection_version",
        "space_id",
        "capability",
        "state",
        "observed_at",
        "mode",
        "source_revision",
        "provider_revision",
    }
)
_NOTIFICATION_FIELDS = frozenset({"space_id", "limit"})


def _error(correlation: Correlation, code: QueryErrorCode, message: str) -> dict[str, Any]:
    return QueryResult(
        status="error",
        trace_id=correlation.trace_id,
        error=QueryError(code=code, message=message),
    ).to_dict()


class ProcessingBindingResolver(Protocol):
    def resolve(self, *, dataset_id: str, space_id: str) -> Mapping[str, Path] | None: ...


class StaticProcessingBindingResolver:
    """Host-owned source bindings; never accepts paths from an HTTP body."""

    def __init__(self, bindings: Mapping[str, Mapping[str, Path]], *, space_id: str | None = None) -> None:
        if bindings and (not isinstance(space_id, str) or not _ID_RE.fullmatch(space_id)):
            raise ValueError("non-empty static bindings require an explicit valid space_id")
        self._bindings = {
            str(dataset_id): {str(source_id): Path(path) for source_id, path in source_paths.items()}
            for dataset_id, source_paths in bindings.items()
        }
        self._space_id = space_id

    def resolve(self, *, dataset_id: str, space_id: str) -> Mapping[str, Path] | None:
        if self._bindings and space_id != self._space_id:
            return None
        value = self._bindings.get(dataset_id)
        return dict(value) if value is not None else None


@dataclass(frozen=True, slots=True)
class GbrainProjectionBinding:
    """Server-side binding for a Wiki publication projection."""

    space_id: str
    asset_id: str
    source_uri: str
    published_uri: str
    source_revision: str
    published_digest: str
    published_markdown: str


def _parse_authoring_request(body: Mapping[str, Any]) -> LogicalDatasetAuthoringRequest:
    source_ids = body.get("source_asset_ids")
    columns = body.get("canonical_columns")
    tags = body.get("tags", [])
    if not isinstance(source_ids, (list, tuple)) or not isinstance(columns, (list, tuple)) or not isinstance(tags, (list, tuple)):
        raise ValueError("source_asset_ids, canonical_columns and tags must be arrays")
    return LogicalDatasetAuthoringRequest(
        dataset_id=body.get("dataset_id"),
        space_id=body.get("space_id"),
        title=body.get("title"),
        source_asset_ids=tuple(source_ids),
        canonical_columns=tuple(columns),
        description=body.get("description", ""),
        schema_mode=body.get("schema_mode", "strict"),
        tags=tuple(tags),
    )


def _parse_semantic_request(body: Mapping[str, Any]) -> SemanticDimensionAuthoringRequest:
    requested_scope = body.get("requested_scope", {})
    input_snapshot = body.get("input_snapshot", {})
    if not isinstance(requested_scope, Mapping) or not isinstance(input_snapshot, Mapping):
        raise ValueError("requested_scope and input_snapshot must be objects")
    return SemanticDimensionAuthoringRequest(
        dimension_id=body.get("dimension_id"),
        space_id=body.get("space_id"),
        adapter=body.get("adapter"),
        requested_scope=requested_scope,
        input_snapshot=input_snapshot,
        title=body.get("title", ""),
    )


def _parse_guardrail_request(body: Mapping[str, Any]) -> SqlGuardrailRule:
    return SqlGuardrailRule(
        id=body.get("id"),
        space_id=body.get("space_id"),
        name=body.get("name"),
        rule_type=body.get("rule_type", body.get("type")),
        scope=body.get("scope", {}),
        params=body.get("params", {}),
        action=body.get("action", "rewrite"),
        message=body.get("message", ""),
        enabled=body.get("enabled", True),
    )


def _parse_semantic_markdown_request(body: Mapping[str, Any]) -> SemanticMarkdownDefinition:
    return SemanticMarkdownDefinition(
        id=body.get("id"),
        space_id=body.get("space_id"),
        semantic_type=body.get("type", body.get("semantic_type")),
        name=body.get("name"),
        description=body.get("description", ""),
        aliases=body.get("aliases", ()),
        tags=body.get("tags", ()),
        frontmatter=body.get("frontmatter", {}),
        body=body.get("body", body.get("markdown", "")),
    )


def _parse_database_binding_request(body: Mapping[str, Any]) -> DatabaseCollectionBindingRequest:
    if set(body) != _DATABASE_BINDING_FIELDS:
        raise ValueError("database Collection binding accepts only portable identity fields")
    return DatabaseCollectionBindingRequest(
        collection_id=body.get("collection_id"),
        collection_version=body.get("collection_version"),
        space_id=body.get("space_id"),
        dataset_id=body.get("dataset_id"),
    )


def _parse_freshness_observation(body: Mapping[str, Any]) -> Any:
    if not set(body).issubset(_FRESHNESS_FIELDS) or not {
        "collection_id",
        "collection_version",
        "space_id",
        "capability",
        "state",
        "observed_at",
    }.issubset(body):
        raise ValueError("Collection freshness observation fields are invalid")
    # Lazy import keeps the Catalog freshness implementation out of legacy
    # probe import graphs until this Admin capability is actually invoked.
    from knowledge_platform.catalog import CollectionFreshnessObservation

    return CollectionFreshnessObservation(
        collection_id=body.get("collection_id"),
        collection_version=body.get("collection_version"),
        space_id=body.get("space_id"),
        capability=body.get("capability"),
        state=body.get("state"),
        observed_at=body.get("observed_at"),
        mode=body.get("mode"),
        source_revision=body.get("source_revision"),
        provider_revision=body.get("provider_revision"),
    )


class RestAdminAdapter:
    """Expose only the Admin definitions and a host-bound Processing publish command."""

    def __init__(
        self,
        *,
        authoring: LogicalDatasetAuthoringService | None,
        processing: LogicalDatasetProcessingService | None,
        bindings: ProcessingBindingResolver,
        processing_worker: LogicalDatasetProcessingWorker | None = None,
        semantic_authoring: SemanticDimensionAuthoringService | None = None,
        semantic_decisions: SemanticDimensionJobDecisionService | None = None,
        semantic_processing: SemanticDimensionBuildWorker | None = None,
        sql_guardrails: SqlGuardrailAdminService | None = None,
        semantic_markdown: SemanticMarkdownAdminService | None = None,
        database_bindings: DatabaseCollectionBindingService | None = None,
        freshness_observations: Any | None = None,
        wiki_compilation: WikiCompilationWorker | None = None,
        capture_processing: CaptureProcessingWorker | None = None,
        connector_sync: ConnectorSyncWorker | None = None,
        connector_sync_paths: Mapping[str, Path] | None = None,
        connector_sync_digests: Mapping[str, str] | None = None,
        gbrain_projection: GbrainProjectionService | None = None,
        gbrain_projection_bindings: Mapping[str, GbrainProjectionBinding] | None = None,
        connector_catalog: Any | None = None,
        asset_upload: LocalAssetUploadService | None = None,
        package_import: LocalPackageImportService | None = None,
        index_rebuild: CatalogIndexRebuildService | None = None,
        connector_authorization: ConnectorAuthorizationService | None = None,
        notifications: NotificationEventQueryService | None = None,
        asset_binding_review_queue: LocalAssetBindingReviewQueue | None = None,
    ) -> None:
        self._authoring = authoring
        self._processing = processing
        self._bindings = bindings
        self._processing_worker = processing_worker
        self._semantic_authoring = semantic_authoring
        self._semantic_decisions = semantic_decisions
        self._semantic_processing = semantic_processing
        self._sql_guardrails = sql_guardrails
        self._semantic_markdown = semantic_markdown
        self._database_bindings = database_bindings
        self._freshness_observations = freshness_observations
        self._wiki_compilation = wiki_compilation
        self._capture_processing = capture_processing
        self._connector_sync = connector_sync
        self._connector_sync_paths = {
            str(source_item_id): Path(path)
            for source_item_id, path in (connector_sync_paths or {}).items()
        }
        self._connector_sync_digests = {
            str(source_item_id): str(digest)
            for source_item_id, digest in (connector_sync_digests or {}).items()
        }
        self._gbrain_projection = gbrain_projection
        self._gbrain_projection_bindings = {
            str(asset_id): binding
            for asset_id, binding in (gbrain_projection_bindings or {}).items()
        }
        self._connector_catalog = connector_catalog
        self._asset_upload = asset_upload
        self._package_import = package_import
        self._index_rebuild = index_rebuild
        self._connector_authorization = connector_authorization
        self._notifications = notifications
        self._asset_binding_review_queue = asset_binding_review_queue

    async def handle(
        self,
        *,
        method: str,
        path: str,
        principal: Principal,
        correlation: Correlation,
        body: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if body is None:
            body = {}
        if not isinstance(body, Mapping):
            return _error(correlation, QueryErrorCode.INVALID_REQUEST, "request body must be an object")
        method = method.upper()
        if method == "POST" and path == "/v1/assets:upload":
            if self._asset_upload is None:
                return _error(correlation, QueryErrorCode.CAPABILITY_UNAVAILABLE, "Asset upload is unavailable")
            try:
                request = AssetUploadRequest.from_mapping(body)
            except (TypeError, ValueError):
                return _error(correlation, QueryErrorCode.INVALID_REQUEST, "Asset upload request is invalid")
            return self._asset_upload.stage(principal=principal, correlation=correlation, request=request).to_dict()
        if method == "POST" and path == "/v1/packages:import":
            if self._package_import is None:
                return _error(correlation, QueryErrorCode.CAPABILITY_UNAVAILABLE, "Package import is unavailable")
            try:
                request = PackageImportRequest.from_mapping(body)
            except (TypeError, ValueError):
                return _error(correlation, QueryErrorCode.INVALID_REQUEST, "Package import request is invalid")
            return self._package_import.stage(principal=principal, correlation=correlation, request=request).to_dict()
        if method == "POST" and path == "/v1/indexes:rebuild":
            if self._index_rebuild is None:
                return _error(correlation, QueryErrorCode.CAPABILITY_UNAVAILABLE, "Index rebuild is unavailable")
            required = {
                "space_id",
                "collection_id",
                "collection_version",
                "capability",
                "provider_id",
                "idempotency_key",
            }
            if set(body) != required:
                return _error(correlation, QueryErrorCode.INVALID_REQUEST, "Index rebuild request is invalid")
            try:
                request = IndexRebuildRequest(**{key: body[key] for key in required})
            except (TypeError, ValueError):
                return _error(correlation, QueryErrorCode.INVALID_REQUEST, "Index rebuild request is invalid")
            return self._index_rebuild.rebuild(principal=principal, correlation=correlation, request=request).to_dict()
        if method == "GET" and path == "/v1/connector-authorizations":
            if self._connector_authorization is None:
                return _error(correlation, QueryErrorCode.CAPABILITY_UNAVAILABLE, "Connector authorization is unavailable")
            return self._connector_authorization.list_status(
                principal=principal, correlation=correlation, space_id=body.get("space_id")
            ).to_dict()
        if method == "GET" and path == "/v1/notifications":
            if self._notifications is None:
                return _error(correlation, QueryErrorCode.CAPABILITY_UNAVAILABLE, "Platform notifications are unavailable")
            if not set(body).issubset(_NOTIFICATION_FIELDS) or "space_id" not in body:
                return _error(correlation, QueryErrorCode.INVALID_REQUEST, "Notification query fields are invalid")
            return self._notifications.list_events(
                principal=principal,
                correlation=correlation,
                space_id=body.get("space_id"),
                limit=body.get("limit", 20),
            ).to_dict()
        if method == "GET" and path == "/v1/asset-binding-reviews":
            if self._asset_binding_review_queue is None:
                return _error(correlation, QueryErrorCode.CAPABILITY_UNAVAILABLE, "local Asset binding review queue is unavailable")
            if set(body) != {"space_id"}:
                return _error(correlation, QueryErrorCode.INVALID_REQUEST, "Asset binding review query fields are invalid")
            return self._asset_binding_review_queue.list(
                principal=principal,
                correlation=correlation,
                space_id=body.get("space_id"),
            ).to_dict()
        authorization_match = _CONNECTOR_AUTH_PATH_RE.fullmatch(path)
        if method == "POST" and authorization_match:
            if self._connector_authorization is None:
                return _error(correlation, QueryErrorCode.CAPABILITY_UNAVAILABLE, "Connector authorization is unavailable")
            if set(body) != _CONNECTOR_AUTH_FIELDS:
                return _error(correlation, QueryErrorCode.INVALID_REQUEST, "Connector authorization fields are invalid")
            try:
                request = ConnectorAuthorizationRequest(
                    space_id=body["space_id"],
                    connector_id=authorization_match.group(1),
                    mode=body["mode"],
                    idempotency_key=body["idempotency_key"],
                )
            except (TypeError, ValueError):
                return _error(correlation, QueryErrorCode.INVALID_REQUEST, "Connector authorization request is invalid")
            return self._connector_authorization.authorize(
                principal=principal, correlation=correlation, request=request
            ).to_dict()
        if method == "POST" and path == "/v1/database/bindings":
            if self._database_bindings is None:
                return _error(correlation, QueryErrorCode.CAPABILITY_UNAVAILABLE, "database Collection binding is unavailable")
            try:
                request = _parse_database_binding_request(body)
            except (TypeError, ValueError):
                return _error(correlation, QueryErrorCode.INVALID_REQUEST, "database Collection binding is invalid")
            return self._database_bindings.bind(
                principal=principal,
                correlation=correlation,
                request=request,
            ).to_dict()
        if method == "POST" and path == "/v1/collections/freshness":
            if self._freshness_observations is None:
                return _error(correlation, QueryErrorCode.CAPABILITY_UNAVAILABLE, "Collection freshness observation is unavailable")
            try:
                observation = _parse_freshness_observation(body)
            except (TypeError, ValueError):
                return _error(correlation, QueryErrorCode.INVALID_REQUEST, "Collection freshness observation is invalid")
            return self._freshness_observations.observe(
                principal=principal,
                correlation=correlation,
                observation=observation,
            ).to_dict()
        if method == "POST" and path == "/v1/database/guardrails":
            if self._sql_guardrails is None:
                return _error(correlation, QueryErrorCode.CAPABILITY_UNAVAILABLE, "SQL guardrail admin is unavailable")
            try:
                request = _parse_guardrail_request(body)
            except (TypeError, ValueError):
                return _error(correlation, QueryErrorCode.INVALID_REQUEST, "SQL guardrail definition is invalid")
            return self._sql_guardrails.create(
                principal=principal,
                correlation=correlation,
                rule=request,
            ).to_dict()
        if method == "GET" and path == "/v1/semantic-assets":
            if self._semantic_markdown is None:
                return _error(correlation, QueryErrorCode.CAPABILITY_UNAVAILABLE, "semantic Markdown admin is unavailable")
            space_id = body.get("space_id")
            status = body.get("status")
            if not isinstance(space_id, str):
                return _error(correlation, QueryErrorCode.INVALID_REQUEST, "space_id is invalid")
            return self._semantic_markdown.discover(principal=principal, correlation=correlation, space_id=space_id, status=status).to_dict()
        if method == "GET" and path == "/v1/connectors":
            if self._connector_catalog is None:
                return _error(correlation, QueryErrorCode.CAPABILITY_UNAVAILABLE, "Connector Catalog is unavailable")
            return self._connector_catalog.list_connectors(
                principal=principal, correlation=correlation, space_id=body.get("space_id")
            ).to_dict()
        if method == "GET" and path == "/v1/source-items":
            if self._connector_catalog is None:
                return _error(correlation, QueryErrorCode.CAPABILITY_UNAVAILABLE, "Source Item Catalog is unavailable")
            return self._connector_catalog.list_source_items(
                principal=principal,
                correlation=correlation,
                space_id=body.get("space_id"),
                connector_id=body.get("connector_id"),
            ).to_dict()
        if method == "POST" and path == "/v1/semantic-assets":
            if self._semantic_markdown is None:
                return _error(correlation, QueryErrorCode.CAPABILITY_UNAVAILABLE, "semantic Markdown admin is unavailable")
            try:
                definition = _parse_semantic_markdown_request(body)
            except (TypeError, ValueError):
                return _error(correlation, QueryErrorCode.INVALID_REQUEST, "semantic Markdown definition is invalid")
            return self._semantic_markdown.prepare(principal=principal, correlation=correlation, definition=definition).to_dict()
        semantic_markdown_match = _SEMANTIC_MARKDOWN_DECISION_PATH_RE.fullmatch(path)
        if method == "POST" and semantic_markdown_match:
            if self._semantic_markdown is None:
                return _error(correlation, QueryErrorCode.CAPABILITY_UNAVAILABLE, "semantic Markdown admin is unavailable")
            space_id = body.get("space_id")
            decision = body.get("decision")
            expected_status = body.get("expected_status")
            if not isinstance(space_id, str) or not isinstance(decision, str) or not isinstance(expected_status, str):
                return _error(correlation, QueryErrorCode.INVALID_REQUEST, "semantic Markdown decision is invalid")
            return self._semantic_markdown.decide(principal=principal, correlation=correlation, asset_id=semantic_markdown_match.group(1), space_id=space_id, decision=decision, expected_status=expected_status).to_dict()
        guardrail_match = _GUARDRAIL_DECISION_PATH_RE.fullmatch(path)
        if method == "POST" and guardrail_match:
            if self._sql_guardrails is None:
                return _error(correlation, QueryErrorCode.CAPABILITY_UNAVAILABLE, "SQL guardrail admin is unavailable")
            space_id = body.get("space_id")
            decision = body.get("decision")
            expected_status = body.get("expected_status")
            if (
                not isinstance(space_id, str)
                or not _ID_RE.fullmatch(space_id)
                or not isinstance(decision, str)
                or not isinstance(expected_status, str)
            ):
                return _error(correlation, QueryErrorCode.INVALID_REQUEST, "SQL guardrail decision is invalid")
            return self._sql_guardrails.decide(
                principal=principal,
                correlation=correlation,
                guardrail_id=guardrail_match.group(1),
                space_id=space_id,
                decision=decision,
                expected_status=expected_status,
            ).to_dict()
        if method == "POST" and path == "/v1/semantic-dimensions":
            if self._semantic_authoring is None:
                return _error(correlation, QueryErrorCode.CAPABILITY_UNAVAILABLE, "semantic authoring is unavailable")
            try:
                request = _parse_semantic_request(body)
            except (TypeError, ValueError):
                return _error(correlation, QueryErrorCode.INVALID_REQUEST, "semantic dimension definition is invalid")
            return self._semantic_authoring.enqueue(
                principal=principal,
                correlation=correlation,
                request=request,
            ).to_dict()
        decision_match = _SEMANTIC_DECISION_PATH_RE.fullmatch(path)
        if method == "POST" and decision_match:
            if self._semantic_decisions is None:
                return _error(correlation, QueryErrorCode.CAPABILITY_UNAVAILABLE, "semantic job decisions are unavailable")
            space_id = body.get("space_id")
            decision = body.get("decision")
            expected_status = body.get("expected_status")
            if (
                not isinstance(space_id, str)
                or not _ID_RE.fullmatch(space_id)
                or not isinstance(decision, str)
                or not isinstance(expected_status, str)
            ):
                return _error(correlation, QueryErrorCode.INVALID_REQUEST, "semantic job decision is invalid")
            try:
                request = SemanticDimensionJobDecisionRequest(
                    job_id=decision_match.group(1),
                    space_id=space_id,
                    decision=decision,
                    expected_status=expected_status,
                )
            except (TypeError, ValueError):
                return _error(correlation, QueryErrorCode.INVALID_REQUEST, "semantic job decision is invalid")
            return self._semantic_decisions.decide(
                principal=principal,
                correlation=correlation,
                request=request,
            ).to_dict()
        process_match = _SEMANTIC_PROCESS_PATH_RE.fullmatch(path)
        if method == "POST" and process_match:
            if self._semantic_processing is None:
                return _error(correlation, QueryErrorCode.CAPABILITY_UNAVAILABLE, "semantic Processing is unavailable")
            if set(body) != {"space_id"} or not isinstance(body.get("space_id"), str) or not _ID_RE.fullmatch(body["space_id"]):
                return _error(correlation, QueryErrorCode.INVALID_REQUEST, "semantic Processing identity is invalid")
            space_id = body["space_id"]
            scopes = set(principal.scopes)
            if principal.tenant_id is not None or not ({"knowledge.admin", "knowledge:admin", "knowledge.processing", "knowledge:processing"} & scopes):
                return _error(correlation, QueryErrorCode.PERMISSION_DENIED, "semantic Processing scope is required")
            if not ({"knowledge.admin", "knowledge:admin"} & scopes) and not ({f"knowledge.space:{space_id}", f"knowledge:space:{space_id}"} & scopes):
                return _error(correlation, QueryErrorCode.PERMISSION_DENIED, "semantic Space scope is required")
            try:
                artifact = self._semantic_processing.process(job_id=process_match.group(1), space_id=space_id)
            except (SemanticDimensionBuildError, LookupError, PermissionError, TypeError, ValueError):
                return _error(correlation, QueryErrorCode.CAPABILITY_UNAVAILABLE, "semantic Processing job failed")
            summary = {
                key: artifact.result_summary.get(key)
                for key in ("adapter", "source_count")
                if key in artifact.result_summary
            }
            return QueryResult(
                status="ok",
                trace_id=correlation.trace_id,
                answer="语义维度已完成处理并进入发布决策边界。",
                data={
                    "job": {
                        "id": process_match.group(1),
                        "state": "artifact_ready",
                    },
                    "artifact": {
                        "staging_uri": artifact.staging_uri,
                        "staging_digest": artifact.staging_digest,
                        "result_summary": summary,
                    },
                },
                evidence=(
                    Evidence(
                        asset_id=process_match.group(1),
                        resource_uri=artifact.staging_uri,
                        locator={"section": "semantic_dimension_staging"},
                        revision=artifact.staging_digest,
                        matched_by=("semantic_processing", "durable_job"),
                    ),
                ),
                provenance=Provenance(
                    space_id=space_id,
                    dataset_id=None,
                    dataset_version=None,
                    capability="semantic_authoring",
                    catalog_revision=None,
                ),
            ).to_dict()
        if method == "POST" and path == "/v1/datasets":
            if self._authoring is None:
                return _error(correlation, QueryErrorCode.CAPABILITY_UNAVAILABLE, "Dataset authoring is unavailable")
            try:
                request = _parse_authoring_request(body)
            except (TypeError, ValueError):
                return _error(correlation, QueryErrorCode.INVALID_REQUEST, "logical dataset definition is invalid")
            return self._authoring.create(
                principal=principal,
                correlation=correlation,
                request=request,
            ).to_dict()
        wiki_compile_match = _WIKI_COMPILE_PATH_RE.fullmatch(path)
        if method == "POST" and wiki_compile_match:
            if self._wiki_compilation is None:
                return _error(correlation, QueryErrorCode.CAPABILITY_UNAVAILABLE, "Wiki compilation is unavailable")
            if not {"knowledge.admin", "knowledge:admin", "knowledge.processing", "knowledge:processing"} & set(
                principal.scopes
            ):
                return _error(correlation, QueryErrorCode.PERMISSION_DENIED, "knowledge.processing scope is required")
            if not {"knowledge.admin", "knowledge:admin"} & set(principal.scopes):
                source_uri = body.get("source_uri")
                source_parts = str(source_uri or "").removeprefix("knowledge://").split("/")
                if (
                    len(source_parts) != 4
                    or source_parts[0] != "spaces"
                    or source_parts[2] != "assets"
                    or not {
                        f"knowledge.space:{source_parts[1]}",
                        f"knowledge:space:{source_parts[1]}",
                    }
                    & set(principal.scopes)
                ):
                    return _error(correlation, QueryErrorCode.PERMISSION_DENIED, "Wiki Space scope is required")
            if set(body) - _WIKI_COMPILE_FIELDS:
                return _error(correlation, QueryErrorCode.INVALID_REQUEST, "Wiki compilation fields are invalid")
            if any(key in body for key in ("path", "source_path", "source_file", "content")):
                return _error(correlation, QueryErrorCode.INVALID_REQUEST, "Wiki source bytes are host-bound")
            required = ("snapshot_id", "source_revision", "source_uri", "content_digest")
            if any(not isinstance(body.get(key), str) or not str(body.get(key)).strip() for key in required):
                return _error(correlation, QueryErrorCode.INVALID_REQUEST, "Wiki compilation identity is invalid")
            if body.get("snapshot_id") != wiki_compile_match.group(1):
                return _error(correlation, QueryErrorCode.INVALID_REQUEST, "Wiki snapshot identity is invalid")
            idempotency_key = body.get("idempotency_key", f"wiki-compile:{wiki_compile_match.group(1)}:{body.get('source_revision')}")
            if not isinstance(idempotency_key, str) or not idempotency_key.strip():
                return _error(correlation, QueryErrorCode.INVALID_REQUEST, "idempotency_key is invalid")
            try:
                compiled = await self._wiki_compilation.compile(
                    WikiCompilationRequest(
                        snapshot_id=body["snapshot_id"],
                        source_revision=body["source_revision"],
                        source_uri=body["source_uri"],
                        content_digest=body["content_digest"],
                        idempotency_key=idempotency_key,
                    )
                )
            except (WikiCompilationError, OSError, TypeError, ValueError):
                return _error(correlation, QueryErrorCode.CAPABILITY_UNAVAILABLE, "Wiki compilation job failed")
            return QueryResult(
                status="ok",
                trace_id=correlation.trace_id,
                answer="Wiki 已完成编译并发布。",
                data={
                    "compilation": {
                        "snapshot_id": compiled.snapshot_id,
                        "source_revision": compiled.source_revision,
                        "resource_uri": compiled.resource_uri,
                        "status": "published",
                    }
                },
                evidence=(
                    Evidence(
                        asset_id=compiled.snapshot_id,
                        resource_uri=compiled.resource_uri,
                        locator={"section": "wiki_compilation"},
                        revision=body["content_digest"],
                        matched_by=("wiki_compile", "durable_job"),
                    ),
                ),
            ).to_dict()
        gbrain_projection_match = _GBRAIN_PROJECTION_PATH_RE.fullmatch(path)
        if method == "POST" and gbrain_projection_match:
            if self._gbrain_projection is None:
                return _error(correlation, QueryErrorCode.CAPABILITY_UNAVAILABLE, "gbrain projection is unavailable")
            if set(body) - _GBRAIN_PROJECTION_FIELDS:
                return _error(correlation, QueryErrorCode.INVALID_REQUEST, "gbrain projection fields are invalid")
            required = ("space_id", "source_revision", "published_digest", "idempotency_key")
            if any(not isinstance(body.get(key), str) or not str(body.get(key)).strip() for key in required):
                return _error(correlation, QueryErrorCode.INVALID_REQUEST, "gbrain projection identity is invalid")
            schema_pack = body.get("schema_pack", "puddingclaw-wiki")
            if not isinstance(schema_pack, str) or not schema_pack.strip():
                return _error(correlation, QueryErrorCode.INVALID_REQUEST, "gbrain schema_pack is invalid")
            asset_id = gbrain_projection_match.group(1)
            binding = self._gbrain_projection_bindings.get(asset_id)
            if (
                binding is None
                or body["space_id"] != binding.space_id
                or body["source_revision"] != binding.source_revision
                or body["published_digest"] != binding.published_digest
            ):
                return _error(correlation, QueryErrorCode.BINDING_UNAVAILABLE, "gbrain projection binding is unavailable")
            scopes = set(principal.scopes)
            if principal.tenant_id is not None or not (
                {"knowledge.admin", "knowledge:admin", "knowledge.processing", "knowledge:processing"} & scopes
            ):
                return _error(correlation, QueryErrorCode.PERMISSION_DENIED, "knowledge.processing scope is required")
            if not ({"knowledge.admin", "knowledge:admin"} & scopes) and not (
                {f"knowledge.space:{binding.space_id}", f"knowledge:space:{binding.space_id}"} & scopes
            ):
                return _error(correlation, QueryErrorCode.PERMISSION_DENIED, "gbrain projection Space scope is required")
            try:
                projected = self._gbrain_projection.project(
                    GbrainProjectionRequest(
                        space_id=binding.space_id,
                        asset_id=binding.asset_id,
                        source_uri=binding.source_uri,
                        published_uri=binding.published_uri,
                        source_revision=binding.source_revision,
                        published_digest=binding.published_digest,
                        published_markdown=binding.published_markdown,
                        idempotency_key=body["idempotency_key"],
                        schema_pack=schema_pack,
                    )
                )
            except (OSError, TypeError, ValueError, LookupError):
                return _error(correlation, QueryErrorCode.CAPABILITY_UNAVAILABLE, "gbrain projection job failed")
            return QueryResult(
                status="ok",
                trace_id=correlation.trace_id,
                answer="gbrain projection 已完成。",
                data={
                    "projection": {
                        "projection_uri": projected.projection_uri,
                        "source_uri": projected.source_uri,
                        "published_uri": projected.published_uri,
                        "source_revision": projected.source_revision,
                        "published_digest": projected.published_digest,
                        "bytes": projected.bytes,
                        "schema_pack": projected.schema_pack,
                        "status": "projected",
                    }
                },
                evidence=(
                    Evidence(
                        asset_id=binding.asset_id,
                        resource_uri=projected.projection_uri,
                        locator={"section": "gbrain_projection"},
                        revision=projected.published_digest,
                        matched_by=("gbrain_projection", "content_addressed"),
                    ),
                ),
                provenance=Provenance(
                    space_id=binding.space_id,
                    dataset_id=None,
                    dataset_version=None,
                    capability="gbrain_projection",
                    catalog_revision=None,
                ),
            ).to_dict()
        capture_process_match = _CAPTURE_PROCESS_PATH_RE.fullmatch(path)
        if method == "POST" and capture_process_match:
            if self._capture_processing is None:
                return _error(correlation, QueryErrorCode.CAPABILITY_UNAVAILABLE, "Capture Processing is unavailable")
            if set(body) - _CAPTURE_PROCESS_FIELDS:
                return _error(correlation, QueryErrorCode.INVALID_REQUEST, "Capture Processing fields are invalid")
            required = tuple(_CAPTURE_PROCESS_FIELDS)
            if any(not isinstance(body.get(key), str) or not str(body.get(key)).strip() for key in required):
                return _error(correlation, QueryErrorCode.INVALID_REQUEST, "Capture Processing identity is invalid")
            source_parts = str(body["source_uri"]).removeprefix("knowledge://").split("/")
            if (
                len(source_parts) != 4
                or source_parts[0] != "spaces"
                or source_parts[2] != "assets"
                or source_parts[3] != capture_process_match.group(1)
            ):
                return _error(correlation, QueryErrorCode.INVALID_REQUEST, "Capture source identity is invalid")
            scopes = set(principal.scopes)
            if principal.tenant_id is not None or not ({"knowledge.admin", "knowledge:admin", "knowledge.processing", "knowledge:processing"} & scopes):
                return _error(correlation, QueryErrorCode.PERMISSION_DENIED, "knowledge.processing scope is required")
            if not ({"knowledge.admin", "knowledge:admin"} & scopes) and not (
                {f"knowledge.space:{source_parts[1]}", f"knowledge:space:{source_parts[1]}"} & scopes
            ):
                return _error(correlation, QueryErrorCode.PERMISSION_DENIED, "Capture Space scope is required")
            try:
                processed = await self._capture_processing.process(
                    CaptureProcessingRequest(
                        asset_id=capture_process_match.group(1),
                        source_revision=body["source_revision"],
                        source_uri=body["source_uri"],
                        content_digest=body["content_digest"],
                        idempotency_key=body["idempotency_key"],
                    )
                )
            except (CaptureProcessingError, OSError, TypeError, ValueError):
                return _error(correlation, QueryErrorCode.CAPABILITY_UNAVAILABLE, "Capture Processing job failed")
            return QueryResult(
                status="ok",
                trace_id=correlation.trace_id,
                answer="Capture 已完成处理并发布。",
                data={
                    "capture": {
                        "asset_id": processed.asset_id,
                        "source_revision": processed.source_revision,
                        "resource_uri": processed.resource_uri,
                        "status": "published",
                    }
                },
                evidence=(
                    Evidence(
                        asset_id=processed.asset_id,
                        resource_uri=processed.resource_uri,
                        locator={"section": "capture_processing"},
                        revision=body["content_digest"],
                        matched_by=("capture_processing", "durable_job"),
                    ),
                ),
                provenance=Provenance(
                    space_id=source_parts[1],
                    dataset_id=None,
                    dataset_version=None,
                    capability="capture_processing",
                    catalog_revision=None,
                ),
            ).to_dict()
        connector_sync_match = _CONNECTOR_SYNC_PATH_RE.fullmatch(path)
        if method == "POST" and connector_sync_match:
            if self._connector_sync is None:
                return _error(correlation, QueryErrorCode.CAPABILITY_UNAVAILABLE, "Connector Sync is unavailable")
            if set(body) - _CONNECTOR_SYNC_FIELDS:
                return _error(correlation, QueryErrorCode.INVALID_REQUEST, "Connector Sync fields are invalid")
            required = tuple(_CONNECTOR_SYNC_FIELDS)
            if any(not isinstance(body.get(key), str) or not str(body.get(key)).strip() for key in required):
                return _error(correlation, QueryErrorCode.INVALID_REQUEST, "Connector Sync identity is invalid")
            connector_id = connector_sync_match.group(1)
            source_item_id = str(body["source_item_id"])
            expected_digest = self._connector_sync_digests.get(source_item_id)
            source_path = self._connector_sync_paths.get(source_item_id)
            if expected_digest is None or source_path is None or body["content_digest"] != expected_digest:
                return _error(correlation, QueryErrorCode.BINDING_UNAVAILABLE, "Connector source binding is unavailable")
            scopes = set(principal.scopes)
            space_id = str(body["space_id"])
            if principal.tenant_id is not None or not (
                {"knowledge.admin", "knowledge:admin", "knowledge.processing", "knowledge:processing"} & scopes
            ):
                return _error(correlation, QueryErrorCode.PERMISSION_DENIED, "knowledge.processing scope is required")
            if not ({"knowledge.admin", "knowledge:admin"} & scopes) and not (
                {f"knowledge.space:{space_id}", f"knowledge:space:{space_id}"} & scopes
            ):
                return _error(correlation, QueryErrorCode.PERMISSION_DENIED, "Connector Space scope is required")
            try:
                synced = self._connector_sync.sync(
                    ConnectorSyncRequest(
                        connector_id=connector_id,
                        space_id=space_id,
                        source_paths={source_item_id: source_path},
                        idempotency_key=body["idempotency_key"],
                    )
                )
            except (ConnectorSyncError, OSError, TypeError, ValueError, LookupError):
                return _error(correlation, QueryErrorCode.CAPABILITY_UNAVAILABLE, "Connector Sync job failed")
            resource_uri = f"knowledge://spaces/{synced.space_id}/source-items/{source_item_id}"
            return QueryResult(
                status="ok",
                trace_id=correlation.trace_id,
                answer="Connector Sync 已完成。",
                data={
                    "sync": {
                        "run_id": synced.run_id,
                        "connector_id": synced.connector_id,
                        "space_id": synced.space_id,
                        "discovered": synced.discovered,
                        "changed": synced.changed,
                        "unchanged": synced.unchanged,
                        "status": "succeeded",
                    }
                },
                evidence=(
                    Evidence(
                        asset_id=source_item_id,
                        resource_uri=resource_uri,
                        locator={"section": "connector_sync"},
                        revision=body["content_digest"],
                        matched_by=("connector_sync", "durable_run"),
                    ),
                ),
                provenance=Provenance(
                    space_id=synced.space_id,
                    dataset_id=None,
                    dataset_version=None,
                    capability="connector_sync",
                    catalog_revision=None,
                ),
            ).to_dict()
        match = _PUBLISH_PATH_RE.fullmatch(path)
        if method == "POST" and match:
            if self._processing is None and self._processing_worker is None:
                return _error(correlation, QueryErrorCode.CAPABILITY_UNAVAILABLE, "Dataset processing is unavailable")
            dataset_id = match.group(1)
            space_id = body.get("space_id")
            if not isinstance(space_id, str) or not _ID_RE.fullmatch(space_id):
                return _error(correlation, QueryErrorCode.INVALID_REQUEST, "space_id is invalid")
            if any(key in body for key in ("source_paths", "source_bindings", "paths")):
                return _error(correlation, QueryErrorCode.INVALID_REQUEST, "source file paths are host-bound")
            bindings = self._bindings.resolve(dataset_id=dataset_id, space_id=space_id)
            if bindings is None:
                return _error(correlation, QueryErrorCode.BINDING_UNAVAILABLE, "source file bindings are unavailable")
            try:
                request = LogicalDatasetProcessingRequest(
                    dataset_id=dataset_id,
                    space_id=space_id,
                    source_paths=bindings,
                )
            except ValueError:
                return _error(correlation, QueryErrorCode.BINDING_UNAVAILABLE, "source file bindings are invalid")
            if self._processing_worker is not None:
                idempotency_key = body.get("idempotency_key", f"admin-publish:{dataset_id}")
                if not isinstance(idempotency_key, str) or not idempotency_key.strip():
                    return _error(correlation, QueryErrorCode.INVALID_REQUEST, "idempotency_key is invalid")
                try:
                    job = await self._processing_worker.process(
                        LogicalDatasetProcessingJobRequest(
                            dataset_id=dataset_id,
                            space_id=space_id,
                            source_paths=bindings,
                            idempotency_key=idempotency_key,
                            principal=principal,
                            correlation=correlation,
                        )
                    )
                except (LogicalDatasetProcessingError, OSError, TypeError, ValueError):
                    return _error(correlation, QueryErrorCode.CAPABILITY_UNAVAILABLE, "logical dataset Processing job failed")
                return QueryResult(
                    status="ok",
                    trace_id=correlation.trace_id,
                    answer="逻辑数据集已完成 source 校验并发布为 ready。",
                    data={
                        "dataset": {
                            "id": job.dataset_id,
                            "space_id": job.space_id,
                            "source_uri": job.resource_uri,
                            "content_digest": job.content_digest,
                            "reference_status": "ready",
                            "row_count": job.row_count,
                        },
                        "job": {"id": job.job_id, "status": "succeeded", "current_step": "completed", "progress": 100},
                    },
                    evidence=(
                        Evidence(
                            asset_id=job.dataset_id,
                            resource_uri=job.resource_uri,
                            locator={"section": "logical_dataset_publication"},
                            revision=job.content_digest,
                            matched_by=("processing", "durable_job"),
                        ),
                    ),
                    provenance=Provenance(
                        space_id=job.space_id,
                        dataset_id=job.dataset_id,
                        dataset_version=None,
                        capability="table_query",
                    ),
                ).to_dict()
            return (
                await self._processing.process(
                    principal=principal,
                    correlation=correlation,
                    request=request,
                )
            ).to_dict()
        return _error(correlation, QueryErrorCode.NOT_FOUND, "Admin endpoint was not found")
