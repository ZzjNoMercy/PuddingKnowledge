from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from knowledge_contracts import Correlation, Principal, QueryResult
from knowledge_platform.capture import CaptureProcessingResult
from knowledge_platform.connector_sync import ConnectorSyncResult
from knowledge_platform.database import (
    DatabaseCollectionBindingService,
    DatabaseDatasetBinding,
    InMemorySqlGuardrailRepository,
    SqlGuardrailAdminService,
)
from knowledge_platform.gbrain import GbrainProjectionResult
from knowledge_platform.semantic import SemanticDimensionBuildArtifact
from knowledge_platform.structured import LogicalDatasetProcessingJobResult
from knowledge_platform.transport import (
    GbrainProjectionBinding,
    RestAdminAdapter,
    StaticProcessingBindingResolver,
    create_admin_router,
)
from knowledge_platform.wiki import WikiCompilationResult


class _Authoring:
    def __init__(self) -> None:
        self.request = None

    def create(self, *, principal, correlation, request):
        self.request = request
        return QueryResult(status="ok", trace_id=correlation.trace_id, data={"dataset_id": request.dataset_id})


class _Processing:
    def __init__(self) -> None:
        self.request = None

    async def process(self, *, principal, correlation, request):
        self.request = request
        return QueryResult(status="ok", trace_id=correlation.trace_id, data={"status": "ready"})


class _ProcessingWorker:
    def __init__(self) -> None:
        self.request = None

    async def process(self, request):
        self.request = request
        return LogicalDatasetProcessingJobResult(
            job_id="logical_process_test",
            dataset_id=request.dataset_id,
            space_id=request.space_id,
            resource_uri=f"knowledge://spaces/{request.space_id}/structured-assets/{request.dataset_id}/source",
            content_digest="sha256:" + "1" * 64,
            row_count=3,
        )


class _WikiWorker:
    def __init__(self) -> None:
        self.request = None

    async def compile(self, request):
        self.request = request
        return WikiCompilationResult(
            snapshot_id=request.snapshot_id,
            source_revision=request.source_revision,
            resource_uri="knowledge://spaces/space_sales/wiki/asset_sales",
        )


class _Semantic:
    def __init__(self) -> None:
        self.request = None

    def enqueue(self, *, principal, correlation, request):
        self.request = request
        return QueryResult(status="ok", trace_id=correlation.trace_id, data={"status": "queued"})


class _SemanticDecision:
    def __init__(self) -> None:
        self.request = None

    def decide(self, *, principal, correlation, request):
        self.request = request
        return QueryResult(status="ok", trace_id=correlation.trace_id, data={"decision": request.decision})


class _SemanticWorker:
    def __init__(self) -> None:
        self.request = None

    def process(self, *, job_id: str, space_id: str):
        self.request = (job_id, space_id)
        return SemanticDimensionBuildArtifact(
            staging_uri=f"knowledge://spaces/{space_id}/semantic-dimensions/dimension/staging",
            staging_digest="sha256:" + "3" * 64,
            result_summary={"adapter": "local", "source_count": 1},
        )


class _CaptureWorker:
    def __init__(self) -> None:
        self.request = None

    async def process(self, request):
        self.request = request
        return CaptureProcessingResult(
            asset_id=request.asset_id,
            source_revision=request.source_revision,
            resource_uri=f"knowledge://spaces/space_sales/captures/{request.asset_id}/content",
        )


class _ConnectorSyncWorker:
    def __init__(self) -> None:
        self.request = None

    def sync(self, request):
        self.request = request
        return ConnectorSyncResult(
            run_id="sync_process_test",
            connector_id=request.connector_id,
            space_id=request.space_id,
            discovered=1,
            changed=0,
            unchanged=1,
        )


class _GbrainProjector:
    def __init__(self) -> None:
        self.request = None

    def project(self, request):
        self.request = request
        return GbrainProjectionResult(
            projection_uri="knowledge://spaces/space_sales/gbrain/asset_sales-abcdef0123456789",
            source_uri=request.source_uri,
            published_uri=request.published_uri,
            source_revision=request.source_revision,
            published_digest=request.published_digest,
            bytes=len(request.published_markdown.encode("utf-8")),
            schema_pack=request.schema_pack,
        )


class _DatabaseResolver:
    def __init__(self, binding: DatabaseDatasetBinding | None) -> None:
        self.binding = binding

    def resolve(self, *, dataset_id: str, space_id: str):
        if self.binding and (self.binding.dataset_id, self.binding.space_id) == (dataset_id, space_id):
            return self.binding
        return None


class _DatabaseWriter:
    def __init__(self) -> None:
        self.request = None

    def bind_collection_provider(self, **kwargs):
        self.request = kwargs
        return {"binding": kwargs["binding"], "path": "/tmp/secret.sqlite3", "password": "secret"}


class _FreshnessObserver:
    def __init__(self) -> None:
        self.observation = None

    def observe(self, *, principal, correlation, observation):
        del principal
        self.observation = observation
        return QueryResult(
            status="ok",
            trace_id=correlation.trace_id,
            data={"collection_freshness": {"collection_id": observation.collection_id}},
        )


def _principal() -> Principal:
    return Principal(subject_id="admin", scopes=("knowledge:admin", "knowledge:space:space_sales"))


@pytest.mark.asyncio
async def test_admin_adapter_creates_definition_and_publishes_only_host_bound_sources(tmp_path: Path) -> None:
    authoring = _Authoring()
    processing = _Processing()
    semantic = _Semantic()
    semantic_decision = _SemanticDecision()
    adapter = RestAdminAdapter(
        authoring=authoring,
        processing=processing,
        bindings=StaticProcessingBindingResolver(
            {"dataset_sales": {"tbl_jan": tmp_path / "jan.csv"}},
            space_id="space_sales",
        ),
        semantic_authoring=semantic,
        semantic_decisions=semantic_decision,
    )
    principal = _principal()
    created = await adapter.handle(
        method="POST",
        path="/v1/datasets",
        principal=principal,
        correlation=Correlation("trace-create"),
        body={
            "dataset_id": "dataset_sales",
            "space_id": "space_sales",
            "title": "Sales",
            "source_asset_ids": ["tbl_jan"],
            "canonical_columns": ["sales"],
        },
    )
    assert created["status"] == "ok"
    assert authoring.request.dataset_id == "dataset_sales"

    published = await adapter.handle(
        method="POST",
        path="/v1/datasets/dataset_sales:publish",
        principal=principal,
        correlation=Correlation("trace-publish"),
        body={"space_id": "space_sales"},
    )
    assert published["status"] == "ok"
    assert processing.request.source_paths == {"tbl_jan": tmp_path / "jan.csv"}

    semantic_result = await adapter.handle(
        method="POST",
        path="/v1/semantic-dimensions",
        principal=principal,
        correlation=Correlation("trace-semantic"),
        body={
            "dimension_id": "vehicle_series",
            "space_id": "space_sales",
            "adapter": "entity_crosswalk_v1",
            "input_snapshot": {"source_asset_ids": ["tbl_jan"]},
        },
    )
    assert semantic_result["status"] == "ok"
    assert semantic.request.dimension_id == "vehicle_series"

    decision_result = await adapter.handle(
        method="POST",
        path="/v1/semantic-dimensions/jobs/authoring_1:decision",
        principal=principal,
        correlation=Correlation("trace-decision"),
        body={
            "space_id": "space_sales",
            "decision": "confirm",
            "expected_status": "waiting_for_publish_confirmation",
        },
    )
    assert decision_result["status"] == "ok"
    assert semantic_decision.request.job_id == "authoring_1"


@pytest.mark.asyncio
async def test_static_processing_bindings_are_fenced_to_their_declared_space(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="explicit valid space_id"):
        StaticProcessingBindingResolver({"dataset_sales": {"tbl_jan": tmp_path / "jan.csv"}})

    processing = _Processing()
    bindings = StaticProcessingBindingResolver(
        {"dataset_sales": {"tbl_jan": tmp_path / "jan.csv"}},
        space_id="space_sales",
    )
    assert bindings.resolve(dataset_id="dataset_sales", space_id="space_sales") == {
        "tbl_jan": tmp_path / "jan.csv"
    }
    assert bindings.resolve(dataset_id="dataset_sales", space_id="space_other") is None

    adapter = RestAdminAdapter(
        authoring=_Authoring(),
        processing=processing,
        bindings=bindings,
    )
    result = await adapter.handle(
        method="POST",
        path="/v1/datasets/dataset_sales:publish",
        principal=Principal("other-space", ("knowledge:admin", "knowledge:space:space_other")),
        correlation=Correlation("trace-cross-space-binding"),
        body={"space_id": "space_other"},
    )
    assert result["error"]["code"] == "binding_unavailable"
    assert processing.request is None


@pytest.mark.asyncio
async def test_admin_adapter_rejects_client_supplied_paths_and_missing_bindings() -> None:
    authoring = _Authoring()
    processing = _Processing()
    adapter = RestAdminAdapter(
        authoring=authoring,
        processing=processing,
        bindings=StaticProcessingBindingResolver({}),
    )
    principal = _principal()
    supplied_path = await adapter.handle(
        method="POST",
        path="/v1/datasets/dataset_sales:publish",
        principal=principal,
        correlation=Correlation("trace-client-path"),
        body={"space_id": "space_sales", "source_paths": {"tbl_jan": "/etc/passwd"}},
    )
    assert supplied_path["error"]["code"] == "invalid_request"
    assert processing.request is None

    missing = await adapter.handle(
        method="POST",
        path="/v1/datasets/dataset_missing:publish",
        principal=principal,
        correlation=Correlation("trace-missing-binding"),
        body={"space_id": "space_sales"},
    )
    assert missing["error"]["code"] == "binding_unavailable"


@pytest.mark.asyncio
async def test_admin_adapter_can_use_durable_processing_worker_without_accepting_paths(tmp_path: Path) -> None:
    worker = _ProcessingWorker()
    adapter = RestAdminAdapter(
        authoring=_Authoring(),
        processing=_Processing(),
        processing_worker=worker,
        bindings=StaticProcessingBindingResolver(
            {"dataset_sales": {"tbl_jan": tmp_path / "jan.csv"}},
            space_id="space_sales",
        ),
    )

    published = await adapter.handle(
        method="POST",
        path="/v1/datasets/dataset_sales:publish",
        principal=_principal(),
        correlation=Correlation("trace-worker-publish"),
        body={"space_id": "space_sales", "idempotency_key": "publish-once"},
    )

    assert published["status"] == "ok"
    assert published["data"]["job"] == {
        "id": "logical_process_test",
        "status": "succeeded",
        "current_step": "completed",
        "progress": 100,
    }
    assert worker.request.idempotency_key == "publish-once"
    assert worker.request.source_paths == {"tbl_jan": tmp_path / "jan.csv"}
    assert "source_paths" not in published


@pytest.mark.asyncio
async def test_admin_adapter_wiki_compile_uses_portable_snapshot_identity_only() -> None:
    worker = _WikiWorker()
    adapter = RestAdminAdapter(
        authoring=_Authoring(),
        processing=_Processing(),
        bindings=StaticProcessingBindingResolver({}),
        wiki_compilation=worker,
    )

    compiled = await adapter.handle(
        method="POST",
        path="/v1/wiki/assets/asset_sales:compile",
        principal=_principal(),
        correlation=Correlation("trace-wiki-compile"),
        body={
            "snapshot_id": "asset_sales",
            "source_revision": "revision-1",
            "source_uri": "knowledge://spaces/space_sales/assets/asset_sales",
            "content_digest": "sha256:" + "2" * 64,
            "idempotency_key": "wiki-compile-once",
        },
    )

    assert compiled["status"] == "ok"
    assert compiled["data"]["compilation"]["status"] == "published"
    assert worker.request.idempotency_key == "wiki-compile-once"
    assert "path" not in compiled


@pytest.mark.asyncio
async def test_admin_adapter_wiki_compile_rejects_unknown_fields_and_missing_processing_scope() -> None:
    worker = _WikiWorker()
    adapter = RestAdminAdapter(
        authoring=_Authoring(),
        processing=_Processing(),
        bindings=StaticProcessingBindingResolver({}),
        wiki_compilation=worker,
    )
    body = {
        "snapshot_id": "asset_sales",
        "source_revision": "revision-1",
        "source_uri": "knowledge://spaces/space_sales/assets/asset_sales",
        "content_digest": "sha256:" + "2" * 64,
        "idempotency_key": "wiki-compile-once",
    }
    unknown = await adapter.handle(
        method="POST",
        path="/v1/wiki/assets/asset_sales:compile",
        principal=_principal(),
        correlation=Correlation("trace-wiki-compile-unknown"),
        body={**body, "token": "must-not-be-ignored"},
    )
    assert unknown["error"]["code"] == "invalid_request"
    assert worker.request is None

    reader = Principal(
        subject_id="reader",
        scopes=("knowledge.search", "knowledge:space:space_sales"),
    )
    unauthorized = await adapter.handle(
        method="POST",
        path="/v1/wiki/assets/asset_sales:compile",
        principal=reader,
        correlation=Correlation("trace-wiki-compile-permission"),
        body=body,
    )
    assert unauthorized["error"]["code"] == "permission_denied"
    assert worker.request is None


@pytest.mark.asyncio
async def test_admin_adapter_semantic_processing_requires_portable_identity_and_scope() -> None:
    worker = _SemanticWorker()
    adapter = RestAdminAdapter(
        authoring=_Authoring(),
        processing=_Processing(),
        bindings=StaticProcessingBindingResolver({}),
        semantic_processing=worker,
    )
    processed = await adapter.handle(
        method="POST",
        path="/v1/semantic-dimensions/jobs/job_1:process",
        principal=_principal(),
        correlation=Correlation("trace-semantic-process"),
        body={"space_id": "space_sales"},
    )
    assert processed["status"] == "ok"
    assert processed["data"]["job"] == {"id": "job_1", "state": "artifact_ready"}
    assert processed["evidence"][0]["resource_uri"].startswith("knowledge://")
    assert worker.request == ("job_1", "space_sales")

    invalid = await adapter.handle(
        method="POST",
        path="/v1/semantic-dimensions/jobs/job_1:process",
        principal=_principal(),
        correlation=Correlation("trace-semantic-process-invalid"),
        body={"space_id": "space_sales", "path": "/tmp/secret"},
    )
    assert invalid["error"]["code"] == "invalid_request"

@pytest.mark.asyncio
async def test_admin_adapter_capture_processing_requires_portable_identity_and_scope() -> None:
    worker = _CaptureWorker()
    adapter = RestAdminAdapter(
        authoring=_Authoring(),
        processing=_Processing(),
        bindings=StaticProcessingBindingResolver({}),
        capture_processing=worker,
    )
    body = {
        "source_revision": "sha256:" + "4" * 64,
        "source_uri": "knowledge://spaces/space_sales/assets/asset_sales",
        "content_digest": "sha256:" + "5" * 64,
        "idempotency_key": "capture-once",
    }
    processed = await adapter.handle(
        method="POST",
        path="/v1/captures/assets/asset_sales:process",
        principal=_principal(),
        correlation=Correlation("trace-capture-process"),
        body=body,
    )
    assert processed["status"] == "ok"
    assert processed["data"]["capture"]["status"] == "published"
    assert processed["provenance"]["capability"] == "capture_processing"
    assert worker.request.asset_id == "asset_sales"

    invalid = await adapter.handle(
        method="POST",
        path="/v1/captures/assets/asset_sales:process",
        principal=_principal(),
        correlation=Correlation("trace-capture-process-invalid"),
        body={**body, "path": "/tmp/secret"},
    )
    assert invalid["error"]["code"] == "invalid_request"

    unauthorized = await adapter.handle(
        method="POST",
        path="/v1/captures/assets/asset_sales:process",
        principal=Principal(subject_id="reader", scopes=("knowledge.search",)),
        correlation=Correlation("trace-capture-process-permission"),
        body=body,
    )
    assert unauthorized["error"]["code"] == "permission_denied"


@pytest.mark.asyncio
async def test_admin_adapter_connector_sync_uses_only_server_side_source_binding(tmp_path: Path) -> None:
    worker = _ConnectorSyncWorker()
    source_item_id = "source_item_1"
    adapter = RestAdminAdapter(
        authoring=_Authoring(),
        processing=_Processing(),
        bindings=StaticProcessingBindingResolver({}),
        connector_sync=worker,
        connector_sync_paths={source_item_id: tmp_path / "source.md"},
        connector_sync_digests={source_item_id: "sha256:" + "6" * 64},
    )
    body = {
        "space_id": "space_sales",
        "source_item_id": source_item_id,
        "content_digest": "sha256:" + "6" * 64,
        "idempotency_key": "connector-sync-once",
    }
    processed = await adapter.handle(
        method="POST",
        path="/v1/sources/connector_sales:sync",
        principal=_principal(),
        correlation=Correlation("trace-connector-sync"),
        body=body,
    )
    assert processed["status"] == "ok"
    assert processed["data"]["sync"]["status"] == "succeeded"
    assert processed["provenance"]["capability"] == "connector_sync"
    assert worker.request.source_paths == {source_item_id: tmp_path / "source.md"}

    unknown = await adapter.handle(
        method="POST",
        path="/v1/sources/connector_sales:sync",
        principal=_principal(),
        correlation=Correlation("trace-connector-sync-unknown"),
        body={**body, "source_path": "/tmp/secret"},
    )
    assert unknown["error"]["code"] == "invalid_request"

    mismatch = await adapter.handle(
        method="POST",
        path="/v1/sources/connector_sales:sync",
        principal=_principal(),
        correlation=Correlation("trace-connector-sync-digest"),
        body={**body, "content_digest": "sha256:" + "7" * 64},
    )
    assert mismatch["error"]["code"] == "binding_unavailable"

    unauthorized = await adapter.handle(
        method="POST",
        path="/v1/sources/connector_sales:sync",
        principal=Principal(subject_id="reader", scopes=("knowledge.search", "knowledge:space:space_sales")),
        correlation=Correlation("trace-connector-sync-permission"),
        body=body,
    )
    assert unauthorized["error"]["code"] == "permission_denied"


@pytest.mark.asyncio
async def test_admin_adapter_gbrain_projection_uses_server_side_markdown_binding() -> None:
    markdown = "# Local Wiki\n"
    digest = "sha256:" + hashlib.sha256(markdown.encode("utf-8")).hexdigest()
    projector = _GbrainProjector()
    binding = GbrainProjectionBinding(
        space_id="space_sales",
        asset_id="asset_sales",
        source_uri="knowledge://spaces/space_sales/assets/asset_sales",
        published_uri="knowledge://spaces/space_sales/wiki/asset_sales",
        source_revision="revision-1",
        published_digest=digest,
        published_markdown=markdown,
    )
    adapter = RestAdminAdapter(
        authoring=_Authoring(),
        processing=_Processing(),
        bindings=StaticProcessingBindingResolver({}),
        gbrain_projection=projector,
        gbrain_projection_bindings={"asset_sales": binding},
    )
    body = {
        "space_id": "space_sales",
        "source_revision": "revision-1",
        "published_digest": digest,
        "idempotency_key": "gbrain-once",
        "schema_pack": "puddingclaw-wiki",
    }
    projected = await adapter.handle(
        method="POST",
        path="/v1/wiki/assets/asset_sales:project-gbrain",
        principal=_principal(),
        correlation=Correlation("trace-gbrain-project"),
        body=body,
    )
    assert projected["status"] == "ok"
    assert projected["data"]["projection"]["status"] == "projected"
    assert projected["provenance"]["capability"] == "gbrain_projection"
    assert projector.request.published_markdown == markdown
    assert "published_markdown" not in body

    invalid = await adapter.handle(
        method="POST",
        path="/v1/wiki/assets/asset_sales:project-gbrain",
        principal=_principal(),
        correlation=Correlation("trace-gbrain-invalid"),
        body={**body, "published_markdown": "# injected"},
    )
    assert invalid["error"]["code"] == "invalid_request"

    invalid_schema = await adapter.handle(
        method="POST",
        path="/v1/wiki/assets/asset_sales:project-gbrain",
        principal=_principal(),
        correlation=Correlation("trace-gbrain-schema-invalid"),
        body={**body, "schema_pack": None},
    )
    assert invalid_schema["error"]["code"] == "invalid_request"

    unauthorized = await adapter.handle(
        method="POST",
        path="/v1/wiki/assets/asset_sales:project-gbrain",
        principal=Principal(subject_id="reader", scopes=("knowledge.search", "knowledge:space:space_sales")),
        correlation=Correlation("trace-gbrain-permission"),
        body=body,
    )
    assert unauthorized["error"]["code"] == "permission_denied"

@pytest.mark.asyncio
async def test_admin_adapter_exposes_guardrail_draft_and_decision_boundary() -> None:
    adapter = RestAdminAdapter(
        authoring=_Authoring(),
        processing=_Processing(),
        bindings=StaticProcessingBindingResolver({}),
        sql_guardrails=SqlGuardrailAdminService(repository=InMemorySqlGuardrailRepository()),
    )
    principal = _principal()
    created = await adapter.handle(
        method="POST",
        path="/v1/database/guardrails",
        principal=principal,
        correlation=Correlation("trace-guardrail-create"),
        body={
            "id": "require_sales_year",
            "space_id": "space_sales",
            "name": "Sales year",
            "rule_type": "require_sql_contains",
            "scope": {"table_scope": {"values": ["sales"]}},
            "params": {"contains": "launch_year"},
            "action": "block",
        },
    )
    assert created["status"] == "ok"
    assert created["data"]["guardrail"]["status"] == "waiting_for_confirmation"

    decided = await adapter.handle(
        method="POST",
        path="/v1/database/guardrails/require_sales_year:decision",
        principal=principal,
        correlation=Correlation("trace-guardrail-decision"),
        body={
            "space_id": "space_sales",
            "decision": "confirm",
            "expected_status": "waiting_for_confirmation",
        },
    )
    assert decided["status"] == "ok"
    assert decided["data"]["guardrail"]["status"] == "active"


def test_fastapi_admin_router_exposes_guardrail_endpoints() -> None:
    adapter = RestAdminAdapter(
        authoring=_Authoring(),
        processing=_Processing(),
        bindings=StaticProcessingBindingResolver({}),
        sql_guardrails=SqlGuardrailAdminService(repository=InMemorySqlGuardrailRepository()),
    )
    principal = _principal()
    app = FastAPI()
    app.include_router(
        create_admin_router(
            adapter,
            principal_provider=lambda: principal,
            correlation_provider=lambda: Correlation("trace-fastapi-guardrail"),
        )
    )
    response = TestClient(app).post(
        "/v1/database/guardrails",
        json={
            "id": "require_sales_year",
            "space_id": "space_sales",
            "name": "Sales year",
            "rule_type": "require_sql_contains",
            "params": {"contains": "launch_year"},
        },
    )
    assert response.status_code == 200
    assert response.json()["data"]["guardrail"]["status"] == "waiting_for_confirmation"


@pytest.mark.asyncio
async def test_admin_adapter_binds_database_dataset_with_portable_ids_only() -> None:
    writer = _DatabaseWriter()
    database_bindings = DatabaseCollectionBindingService(
        datasets=_DatabaseResolver(
            DatabaseDatasetBinding(
                dataset_id="dataset_sales",
                space_id="space_sales",
                dataset_version="v1",
                deployment_revision="deploy-1",
                dialect="postgresql",
                allowed_tables=("sales",),
                semantic_context_hash="sha256:" + "a" * 64,
                source_revision="sha256:" + "b" * 64,
                provider_version="local-postgresql",
            )
        ),
        writer=writer,
    )
    adapter = RestAdminAdapter(
        authoring=_Authoring(),
        processing=_Processing(),
        bindings=StaticProcessingBindingResolver({}),
        database_bindings=database_bindings,
    )
    principal = _principal()
    result = await adapter.handle(
        method="POST",
        path="/v1/database/bindings",
        principal=principal,
        correlation=Correlation("trace-database-binding"),
        body={
            "collection_id": "collection_sales",
            "collection_version": "v1",
            "space_id": "space_sales",
            "dataset_id": "dataset_sales",
        },
    )
    assert result["status"] == "ok"
    assert writer.request["binding"] == {"dataset_id": "dataset_sales"}
    assert result["evidence"][0]["revision"] == "sha256:" + "b" * 64

    rejected = await adapter.handle(
        method="POST",
        path="/v1/database/bindings",
        principal=principal,
        correlation=Correlation("trace-database-binding-path"),
        body={
            "collection_id": "collection_sales",
            "collection_version": "v1",
            "space_id": "space_sales",
            "dataset_id": "dataset_sales",
            "password": "secret",
        },
    )
    assert rejected["error"]["code"] == "invalid_request"

    tenant_denied = await adapter.handle(
        method="POST",
        path="/v1/database/bindings",
        principal=Principal(
            subject_id="admin",
            tenant_id="tenant-a",
            scopes=("knowledge:admin", "knowledge:space:space_sales"),
        ),
        correlation=Correlation("trace-database-binding-tenant"),
        body={
            "collection_id": "collection_sales",
            "collection_version": "v1",
            "space_id": "space_sales",
            "dataset_id": "dataset_sales",
        },
    )
    assert tenant_denied["error"]["code"] == "permission_denied"


@pytest.mark.asyncio
async def test_admin_adapter_accepts_only_portable_collection_freshness_observation() -> None:
    observer = _FreshnessObserver()
    adapter = RestAdminAdapter(
        authoring=_Authoring(),
        processing=_Processing(),
        bindings=StaticProcessingBindingResolver({}),
        freshness_observations=observer,
    )
    result = await adapter.handle(
        method="POST",
        path="/v1/collections/freshness",
        principal=_principal(),
        correlation=Correlation("trace-freshness-admin"),
        body={
            "collection_id": "collection_sales",
            "collection_version": "v1",
            "space_id": "space_sales",
            "capability": "wiki_query",
            "state": "ready",
            "observed_at": "2026-09-04T00:00:00+00:00",
            "mode": "local_published_wiki",
            "source_revision": "local-wiki-1",
        },
    )
    assert result["status"] == "ok"
    assert observer.observation.collection_id == "collection_sales"
    assert observer.observation.source_revision == "local-wiki-1"

    rejected = await adapter.handle(
        method="POST",
        path="/v1/collections/freshness",
        principal=_principal(),
        correlation=Correlation("trace-freshness-path"),
        body={
            "collection_id": "collection_sales",
            "collection_version": "v1",
            "space_id": "space_sales",
            "capability": "wiki_query",
            "state": "ready",
            "observed_at": "2026-09-04T00:00:00+00:00",
            "path": "/Users/pet/private/wiki",
        },
    )
    assert rejected["error"]["code"] == "invalid_request"


def test_fastapi_admin_router_exposes_collection_freshness() -> None:
    observer = _FreshnessObserver()
    app = FastAPI()
    app.include_router(
        create_admin_router(
            RestAdminAdapter(
                authoring=_Authoring(),
                processing=_Processing(),
                bindings=StaticProcessingBindingResolver({}),
                freshness_observations=observer,
            ),
            principal_provider=lambda: _principal(),
            correlation_provider=lambda: Correlation("trace-fastapi-freshness"),
        )
    )
    response = TestClient(app).post(
        "/v1/collections/freshness",
        json={
            "collection_id": "collection_sales",
            "collection_version": "v1",
            "space_id": "space_sales",
            "capability": "wiki_query",
            "state": "ready",
            "observed_at": "2026-09-04T00:00:00+00:00",
        },
    )
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_fastapi_admin_router_exposes_connector_sync() -> None:
    worker = _ConnectorSyncWorker()
    source_item_id = "source_item_1"
    app = FastAPI()
    app.include_router(
        create_admin_router(
            RestAdminAdapter(
                authoring=_Authoring(),
                processing=_Processing(),
                bindings=StaticProcessingBindingResolver({}),
                connector_sync=worker,
                connector_sync_paths={source_item_id: Path("/tmp/source.md")},
                connector_sync_digests={source_item_id: "sha256:" + "6" * 64},
            ),
            principal_provider=lambda: _principal(),
            correlation_provider=lambda: Correlation("trace-fastapi-connector-sync"),
        )
    )
    response = TestClient(app).post(
        "/v1/sources/connector_sales:sync",
        json={
            "space_id": "space_sales",
            "source_item_id": source_item_id,
            "content_digest": "sha256:" + "6" * 64,
            "idempotency_key": "connector-sync-once",
        },
    )
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_fastapi_admin_router_exposes_gbrain_projection() -> None:
    markdown = "# Local Wiki\n"
    digest = "sha256:" + hashlib.sha256(markdown.encode("utf-8")).hexdigest()
    projector = _GbrainProjector()
    app = FastAPI()
    app.include_router(
        create_admin_router(
            RestAdminAdapter(
                authoring=_Authoring(),
                processing=_Processing(),
                bindings=StaticProcessingBindingResolver({}),
                gbrain_projection=projector,
                gbrain_projection_bindings={
                    "asset_sales": GbrainProjectionBinding(
                        space_id="space_sales",
                        asset_id="asset_sales",
                        source_uri="knowledge://spaces/space_sales/assets/asset_sales",
                        published_uri="knowledge://spaces/space_sales/wiki/asset_sales",
                        source_revision="revision-1",
                        published_digest=digest,
                        published_markdown=markdown,
                    )
                },
            ),
            principal_provider=lambda: _principal(),
            correlation_provider=lambda: Correlation("trace-fastapi-gbrain"),
        )
    )
    response = TestClient(app).post(
        "/v1/wiki/assets/asset_sales:project-gbrain",
        json={
            "space_id": "space_sales",
            "source_revision": "revision-1",
            "published_digest": digest,
            "idempotency_key": "gbrain-once",
        },
    )
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_fastapi_admin_router_exposes_database_collection_binding() -> None:
    writer = _DatabaseWriter()
    database_bindings = DatabaseCollectionBindingService(
        datasets=_DatabaseResolver(
            DatabaseDatasetBinding(
                dataset_id="dataset_sales",
                space_id="space_sales",
                dataset_version="v1",
                deployment_revision="deploy-1",
                dialect="postgresql",
                allowed_tables=("sales",),
                semantic_context_hash="sha256:" + "a" * 64,
                source_revision="sha256:" + "b" * 64,
            )
        ),
        writer=writer,
    )
    app = FastAPI()
    app.include_router(
        create_admin_router(
            RestAdminAdapter(
                authoring=_Authoring(),
                processing=_Processing(),
                bindings=StaticProcessingBindingResolver({}),
                database_bindings=database_bindings,
            ),
            principal_provider=lambda: _principal(),
            correlation_provider=lambda: Correlation("trace-fastapi-database-binding"),
        )
    )
    response = TestClient(app).post(
        "/v1/database/bindings",
        json={
            "collection_id": "collection_sales",
            "collection_version": "v1",
            "space_id": "space_sales",
            "dataset_id": "dataset_sales",
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert "password" not in response.text.casefold()


@pytest.mark.asyncio
async def test_admin_adapter_reads_only_explicitly_space_bound_notifications(tmp_path: Path) -> None:
    from sqlalchemy import create_engine

    from knowledge_contracts import NotificationEvent
    from knowledge_platform.catalog import migrate_to_latest
    from knowledge_platform.catalog.notification_scope import SqliteNotificationEventScopeStore
    from knowledge_platform.catalog.notification_service import NotificationEventQueryService

    database = tmp_path / "notifications.sqlite3"
    engine = create_engine(f"sqlite:///{database}")
    with engine.begin() as connection:
        migrate_to_latest(connection)
    engine.dispose()
    store = SqliteNotificationEventScopeStore(database)
    store.publish(
        event=NotificationEvent(
            event_id="notification_admin_1",
            event_type="task_notification.v1",
            subject_type="job",
            subject_id="job_1",
            title="Processing complete",
            occurred_at="2026-09-05T00:00:00Z",
            payload={"status": "published"},
        ),
        category="processing",
        space_id="space_sales",
    )
    adapter = RestAdminAdapter(
        authoring=_Authoring(),
        processing=_Processing(),
        bindings=StaticProcessingBindingResolver({}),
        notifications=NotificationEventQueryService(store),
    )
    response = await adapter.handle(
        method="GET",
        path="/v1/notifications",
        principal=_principal(),
        correlation=Correlation("trace-notifications"),
        body={"space_id": "space_sales", "limit": 10},
    )
    assert response["status"] == "ok"
    assert response["data"]["notifications"][0]["space_id"] == "space_sales"
    rejected = await adapter.handle(
        method="GET",
        path="/v1/notifications",
        principal=_principal(),
        correlation=Correlation("trace-notifications-invalid"),
        body={"space_id": "space_sales", "path": "/Users/pet/private"},
    )
    assert rejected["error"]["code"] == "invalid_request"


def test_fastapi_admin_router_exposes_space_bound_notifications(tmp_path: Path) -> None:
    from sqlalchemy import create_engine

    from knowledge_contracts import NotificationEvent
    from knowledge_platform.catalog import migrate_to_latest
    from knowledge_platform.catalog.notification_scope import SqliteNotificationEventScopeStore
    from knowledge_platform.catalog.notification_service import NotificationEventQueryService

    database = tmp_path / "notifications-fastapi.sqlite3"
    engine = create_engine(f"sqlite:///{database}")
    with engine.begin() as connection:
        migrate_to_latest(connection)
    engine.dispose()
    store = SqliteNotificationEventScopeStore(database)
    store.publish(
        event=NotificationEvent(
            event_id="notification_fastapi_1",
            event_type="task_notification.v1",
            subject_type="job",
            subject_id="job_1",
            title="Processing complete",
            occurred_at="2026-09-05T00:00:00Z",
            payload={"status": "published"},
        ),
        category="processing",
        space_id="space_sales",
    )
    app = FastAPI()
    app.include_router(
        create_admin_router(
            RestAdminAdapter(
                authoring=_Authoring(),
                processing=_Processing(),
                bindings=StaticProcessingBindingResolver({}),
                notifications=NotificationEventQueryService(store),
            ),
            principal_provider=lambda: _principal(),
            correlation_provider=lambda: Correlation("trace-fastapi-notifications"),
        )
    )
    response = TestClient(app).get("/v1/notifications?space_id=space_sales&limit=5")
    assert response.status_code == 200
    assert response.json()["data"]["count"] == 1
