"""Serve the local Platform edge for the Phase 8 loopback-process shadow.

This is a deliberately small shadow-only entry point.  It receives an explicit
staged Catalog and Wiki root, materializes them into an isolated temporary
directory, and starts Uvicorn on loopback.  It has no production discovery,
credential loading, or activation path.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import itertools
import json
import os
import shutil
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import uvicorn
from fastapi.middleware.cors import CORSMiddleware

from knowledge_contracts import Correlation, Evidence, Principal
from knowledge_platform.capture import (
    CaptureProcessingWorker,
    LocalCapturePublishingService,
    SqliteCaptureProcessingJobStore,
)
from knowledge_platform.catalog import (
    CatalogQueryService,
    SqliteCatalogQueryRepository,
    SqliteLogicalDatasetProcessingJobStore,
    SqliteSemanticDimensionJobStore,
    SqliteSemanticDimensionJobWriter,
    SqliteStructuredAssetWriter,
)
from knowledge_platform.catalog.connector_authorization import ConnectorAuthorizationService
from knowledge_platform.catalog.connector_queries import CatalogConnectorQueryService
from knowledge_platform.catalog.deployment import (
    DeploymentActivationController,
    DeploymentArtifact,
    DeploymentManifest,
)
from knowledge_platform.catalog.deployment_sqlite import SqliteDeploymentActivationStore
from knowledge_platform.catalog.index_rebuild import CatalogIndexRebuildService
from knowledge_platform.catalog.local_asset_binding_review_queue import LocalAssetBindingReviewQueue
from knowledge_platform.catalog.notification_scope import SqliteNotificationEventScopeStore
from knowledge_platform.catalog.notification_service import NotificationEventQueryService
from knowledge_platform.catalog.vector_rebuild import VectorRebuildManifest
from knowledge_platform.connector_sync import (
    ConnectorSyncWorker,
    LocalConnectorSourceProvider,
    SqliteConnectorSyncStore,
)
from knowledge_platform.database import (
    DatabaseCollectionBindingRequest,
    DatabaseCollectionBindingService,
    DatabaseExecuteReadonlyService,
    DatabaseNl2SqlService,
    DatabaseSqlCandidate,
    GatewayVannaProvider,
    InMemoryQueryPlanRepository,
    LocalPostgresDatabaseDatasetResolver,
    LocalPostgresDatabaseSchemaReader,
    LocalPostgresDatabaseSource,
    LocalVannaCollectionGateway,
    PostgresReadonlyDatabaseExecutor,
    PostgresReadonlySqlValidator,
    StaticNl2SqlProvider,
)
from knowledge_platform.database.schema_service import DatabaseSchemaQueryService
from knowledge_platform.gbrain import LocalGbrainProjectionService
from knowledge_platform.ingestion import LocalAssetUploadService, LocalPackageImportService
from knowledge_platform.retrieval import (
    AssetReadService,
    CatalogSearchService,
    DocumentRetrievalService,
    LocalFilesystemBlobReader,
    LocalPublishedWikiProvider,
    MilvusBm25CatalogRetrievalProvider,
    WikiQueryService,
)
from knowledge_platform.retrieval.local_transformers_embedding import LocalTransformersEmbeddingClient
from knowledge_platform.retrieval.milvus import MilvusCatalogRetrievalProvider
from knowledge_platform.router import KnowledgeQueryRouter, build_local_query_engines
from knowledge_platform.semantic import (
    LocalSemanticDimensionBuilder,
    LocalSemanticDimensionPublisher,
    SemanticDimensionBuildWorker,
    SemanticDimensionJobDecisionService,
    SemanticMarkdownAdminService,
    SqliteSemanticMarkdownRepository,
)
from knowledge_platform.structured import (
    LocalStructuredFileBindingVerifier,
    LocalStructuredFileProvider,
    LogicalDatasetAuthoringService,
    LogicalDatasetProcessingService,
    LogicalDatasetProcessingWorker,
    StructuredAssetBindingRequest,
    StructuredAssetBindingService,
    TableQueryService,
)
from knowledge_platform.transport import (
    GbrainProjectionBinding,
    McpQueryAdapter,
    RestAdminAdapter,
    RestQueryAdapter,
    StaticProcessingBindingResolver,
    create_platform_app,
)
from knowledge_platform.wiki import (
    BoundedWikiContextService,
    DeterministicWikiModelGateway,
    LocalImmutableRawSnapshotRepository,
    LocalWikiDraftValidator,
    LocalWikiPublishingService,
    SqliteWikiCompilationJobStore,
    WikiCompilationWorker,
)
from scripts.phase6_local_wiki_query_shadow import _materialize_catalog
from scripts.phase8_local_platform_http_shadow import _build_app

_SPACE_ID = "space_kb_default"
_COLLECTION_ID = "dataset_kb_default"
_COLLECTION_VERSION = "legacy-73c066b66ad712df"
_PROVIDER_ID = "puddingclaw_platform_candidate_lexical_text"
_DENSE_PROVIDER_ID = "puddingclaw_platform_candidate_text"
_TABLE_SPACE_ID = "space_kb_default"
_TABLE_COLLECTION_ID = "dataset_kb_default"
_DATABASE_DATASET_ID = "database_insight_data_vehicle_model_base"
_DATABASE_TABLE = "vehicle_model_base"
_DATABASE_QUESTION = "统计本地车型能源类型数量"
_LOGICAL_SOURCE_ID = "phase8_process_source"
_LOGICAL_DATASET_ID = "phase8_process_logical_dataset"
_SEMANTIC_JOB_ID = "phase8_process_semantic_job"
_SEMANTIC_DIMENSION_ID = "phase8_process_semantic_dimension"
_CONNECTOR_ID = "connector_src_b8579e2221b45d9a35af5098"
_CONNECTOR_SOURCE_ITEM_ID = "source_item_sitem_675bab9905991ba3dbe4e946"
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _validate_dense_loopback_uri(uri: str) -> str:
    parsed = urlparse(uri) if isinstance(uri, str) else None
    if (
        parsed is None
        or parsed.scheme not in {"http", "https"}
        or parsed.hostname not in _LOOPBACK_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or any(ord(character) < 32 for character in uri)
    ):
        raise ValueError("dense process Milvus URI must be an explicit loopback URL")
    return uri


def _validate_dense_manifest_path(path: Path) -> Path:
    absolute = path.expanduser().absolute()
    current = absolute
    while current != current.parent:
        if current.is_symlink():
            raise ValueError("dense process manifest must not contain symlink components")
        current = current.parent
    if not absolute.is_file():
        raise FileNotFoundError(absolute)
    return absolute


def _local_database_schema_evidence(*, source_revision: str) -> tuple[Evidence, ...]:
    """Return portable schema evidence for the deterministic local shadow SQL."""

    return (
        Evidence(
            asset_id="db_schema_vehicle_model_base",
            resource_uri=(
                f"knowledge://spaces/{_SPACE_ID}/databases/{_DATABASE_DATASET_ID}/schema/{_DATABASE_TABLE}"
            ),
            locator={"section": "schema"},
            quote="vehicle_model_base exposes energy_type for the grouped model count query.",
            revision=source_revision,
            matched_by=("local_schema",),
        ),
    )


def _merge_explicit_asset_bindings(
    materialized_bindings: Mapping[str, Path], explicit_bindings: Mapping[str, Path]
) -> dict[str, Path]:
    """Merge Host-approved bindings without silently replacing a path."""

    merged = {str(asset_id): Path(path) for asset_id, path in materialized_bindings.items()}
    for asset_id, path in explicit_bindings.items():
        key = str(asset_id)
        candidate = Path(path).expanduser().absolute()
        existing = merged.get(key)
        if existing is not None and existing.expanduser().absolute() != candidate:
            raise ValueError(f"Asset {key} has conflicting local path bindings")
        merged[key] = candidate
    return merged


def _bounded_csv(source: Path, target: Path) -> None:
    """Create the same bounded local fixture used by Processing shadows."""

    try:
        import openpyxl
    except ImportError as error:
        raise RuntimeError("openpyxl is required for the local logical shadow") from error
    workbook = openpyxl.load_workbook(source, read_only=True, data_only=True)
    try:
        sheet = workbook[workbook.sheetnames[0]]
        rows = list(itertools.islice(sheet.iter_rows(values_only=True), 4))
    finally:
        workbook.close()
    if len(rows) < 2:
        raise ValueError("local source does not contain a header and data row")
    header = [str(value or "").strip() for value in rows[0]]
    if not header or any(not value for value in header) or len(set(header)) != len(header):
        raise ValueError("local source header is not a bounded tabular schema")
    with target.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(header)
        for row in rows[1:]:
            writer.writerow(["" if value is None else str(value) for value in row])


def _stage_logical_source(catalog: Path, *, source_path: Path, profile: object) -> None:
    """Stage only path-free source metadata into the isolated Catalog copy."""

    now = datetime.now(UTC).isoformat()
    columns = list(getattr(profile, "columns"))
    uri = f"knowledge://spaces/{_SPACE_ID}/structured-assets/{_LOGICAL_SOURCE_ID}/source"
    with sqlite3.connect(catalog) as connection:
        connection.execute(
            """
            INSERT INTO knowledge_structured_assets (
                id, space_id, source_key, document_asset_id, source_type, file_name, sheet_name,
                size_bytes, modified_at, source_uri, source_reference_digest, logical_path_digest,
                profile_uri, profile_reference_digest, content_digest, profile_status, row_count,
                column_count, columns_json, reference_status, capabilities, metadata_json, created_at, updated_at
            ) VALUES (?, ?, ?, NULL, 'local', ?, NULL, ?, NULL, ?, '', '', ?, '', ?, 'missing', NULL, ?, ?, 'pending', ?, '{}', ?, ?)
            """,
            (
                _LOGICAL_SOURCE_ID,
                _SPACE_ID,
                f"shadow:{_LOGICAL_SOURCE_ID}",
                source_path.name,
                source_path.stat().st_size,
                uri,
                uri.replace("/source", "/profile"),
                str(getattr(profile, "content_digest")),
                len(columns),
                json.dumps(columns, ensure_ascii=False),
                json.dumps(["table_query"]),
                now,
                now,
            ),
        )


def _digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _validate_console_origin(origin: str) -> str:
    """Allow only an explicit HTTP loopback origin for local Console testing."""

    candidate = origin.strip()
    parsed = urlparse(candidate)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.path not in {"", "/"}
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("console origin must be an explicit http loopback origin")
    return candidate.rstrip("/")


def _configure_console_cors(app: object, origin: str) -> None:
    """Mount narrowly scoped CORS for an explicitly requested local Console."""

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[_validate_console_origin(origin)],
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["content-type"],
    )


def _has_table(database_path: Path, table_name: str) -> bool:
    with sqlite3.connect(database_path) as connection:
        return connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone() is not None


def _deployment_controller(
    *, temporary_catalog: Path, temp_dir: Path, provider_id: str
) -> DeploymentActivationController:
    legacy_revision = "legacy-local-process-v1"
    candidate_revision = "platform-local-process-v1"

    def manifest(revision: str) -> DeploymentManifest:
        return DeploymentManifest(
            deployment_revision=revision,
            artifacts=(
                DeploymentArtifact("catalog", revision, "local-catalog", _digest(str(temporary_catalog))),
                DeploymentArtifact("blob", revision, "local-blob-tree", _digest("local-blob-tree")),
                DeploymentArtifact("vector_index", revision, provider_id, _digest(provider_id)),
                DeploymentArtifact("wiki_root", revision, "local-wiki-root", _digest("local-wiki-root")),
            ),
        )

    controller = DeploymentActivationController(
        store=SqliteDeploymentActivationStore(temp_dir / "deployment.sqlite3")
    )
    legacy = manifest(legacy_revision)
    candidate = manifest(candidate_revision)
    controller.prepare(
        installation_id="phase8-local-platform-process",
        legacy_manifest=legacy,
        candidate_manifest=candidate,
    )
    controller.mark_drained(proof_digest=_digest("phase8-local-process-drain"))
    controller.verify(
        legacy_before_digest=legacy.manifest_digest(),
        legacy_after_digest=legacy.manifest_digest(),
        candidate_manifest_digest=candidate.manifest_digest(),
        checks={"legacy_unchanged": True, "candidate_manifest_matches": True, "bundle_integrity": True},
    )
    controller.activate(deployment_revision=candidate_revision)
    return controller


def _build_lexical_app(
    repository: SqliteCatalogQueryRepository,
    provider: MilvusBm25CatalogRetrievalProvider,
    principal: Principal,
    deployment: DeploymentActivationController,
):
    catalog = CatalogQueryService(repository)
    document = DocumentRetrievalService(provider, repository)
    wiki = WikiQueryService(provider, repository)
    rest = RestQueryAdapter(
        catalog=catalog,
        search=CatalogSearchService(catalog),
        asset_read=AssetReadService(catalog=repository, reader=LocalFilesystemBlobReader({})),
        document=document,
        wiki=wiki,
        knowledge_query=KnowledgeQueryRouter(
            catalog=repository,
            engines=build_local_query_engines(document=document, document_provider_id=_PROVIDER_ID),
        ),
        deployment=deployment,
    )
    return create_platform_app(
        query_adapter=rest,
        mcp_adapter=McpQueryAdapter(rest),
        principal_provider=lambda: principal,
        correlation_provider=lambda: Correlation("phase8-local-platform-process-lexical-shadow"),
    )


def _build_dense_app(
    repository: SqliteCatalogQueryRepository,
    provider: MilvusCatalogRetrievalProvider,
    principal: Principal,
    deployment: DeploymentActivationController,
):
    catalog = CatalogQueryService(repository)
    document = DocumentRetrievalService(provider, repository)
    wiki = WikiQueryService(provider, repository)
    rest = RestQueryAdapter(
        catalog=catalog,
        search=CatalogSearchService(catalog),
        asset_read=AssetReadService(catalog=repository, reader=LocalFilesystemBlobReader({})),
        document=document,
        wiki=wiki,
        knowledge_query=KnowledgeQueryRouter(
            catalog=repository,
            engines=build_local_query_engines(document=document, document_provider_id=_DENSE_PROVIDER_ID),
        ),
        deployment=deployment,
    )
    return create_platform_app(
        query_adapter=rest,
        mcp_adapter=McpQueryAdapter(rest),
        principal_provider=lambda: principal,
        correlation_provider=lambda: Correlation("phase8-local-platform-process-dense-shadow"),
    )


def _build_table_app(
    repository: SqliteCatalogQueryRepository,
    table: TableQueryService,
    principal: Principal,
    deployment: DeploymentActivationController,
):
    catalog = CatalogQueryService(repository)
    wiki_provider = LocalPublishedWikiProvider(catalog=repository, asset_paths={})
    document = DocumentRetrievalService(wiki_provider, repository)
    wiki = WikiQueryService(wiki_provider, repository)
    rest = RestQueryAdapter(
        catalog=catalog,
        search=CatalogSearchService(catalog),
        asset_read=AssetReadService(catalog=repository, reader=LocalFilesystemBlobReader({})),
        document=document,
        wiki=wiki,
        table=table,
        knowledge_query=KnowledgeQueryRouter(
            catalog=repository,
            engines=build_local_query_engines(table=table),
        ),
        deployment=deployment,
    )
    return create_platform_app(
        query_adapter=rest,
        mcp_adapter=McpQueryAdapter(rest),
        principal_provider=lambda: principal,
        correlation_provider=lambda: Correlation("phase8-local-platform-process-table-shadow"),
    )


def _build_logical_app(
    repository: SqliteCatalogQueryRepository,
    table: TableQueryService,
    authoring: LogicalDatasetAuthoringService,
    processing: LogicalDatasetProcessingService,
    worker: LogicalDatasetProcessingWorker,
    bindings: StaticProcessingBindingResolver,
    principal: Principal,
    deployment: DeploymentActivationController,
):
    catalog = CatalogQueryService(repository)
    wiki_provider = LocalPublishedWikiProvider(catalog=repository, asset_paths={})
    document = DocumentRetrievalService(wiki_provider, repository)
    wiki = WikiQueryService(wiki_provider, repository)
    rest = RestQueryAdapter(
        catalog=catalog,
        search=CatalogSearchService(catalog),
        asset_read=AssetReadService(catalog=repository, reader=LocalFilesystemBlobReader({})),
        document=document,
        wiki=wiki,
        table=table,
        knowledge_query=KnowledgeQueryRouter(
            catalog=repository,
            engines=build_local_query_engines(table=table),
        ),
        deployment=deployment,
    )
    admin = RestAdminAdapter(
        authoring=authoring,
        processing=processing,
        processing_worker=worker,
        bindings=bindings,
    )
    return create_platform_app(
        query_adapter=rest,
        admin_adapter=admin,
        mcp_adapter=McpQueryAdapter(rest),
        principal_provider=lambda: principal,
        correlation_provider=lambda: Correlation("phase8-local-platform-process-logical-shadow"),
    )


def _build_wiki_compile_app(
    repository: SqliteCatalogQueryRepository,
    worker: WikiCompilationWorker,
    asset_id: str,
    output_path: Path,
    principal: Principal,
    deployment: DeploymentActivationController,
):
    catalog = CatalogQueryService(repository)

    class _CompiledWikiCatalog:
        @property
        def catalog_revision(self) -> str:
            return repository.catalog_revision

        def list_assets(self, *, space_id: str | None = None):
            if not output_path.is_file() or (space_id is not None and space_id != _SPACE_ID):
                return []
            digest = "sha256:" + hashlib.sha256(output_path.read_bytes()).hexdigest()
            return [
                {
                    "id": asset_id,
                    "space_id": _SPACE_ID,
                    "kind": "wiki_page",
                    "title": asset_id,
                    "source_uri": f"knowledge://spaces/{_SPACE_ID}/assets/{asset_id}",
                    "content_digest": digest,
                    "revision": digest,
                }
            ]

        def get_asset(self, *, asset_id: str):
            return next(
                (asset for asset in self.list_assets(space_id=_SPACE_ID) if asset["id"] == asset_id),
                None,
            )

    wiki_provider = LocalPublishedWikiProvider(
        catalog=_CompiledWikiCatalog(), asset_paths={asset_id: output_path}
    )
    compiled_catalog = _CompiledWikiCatalog()
    document = DocumentRetrievalService(wiki_provider, compiled_catalog)
    wiki = WikiQueryService(wiki_provider, compiled_catalog)
    rest = RestQueryAdapter(
        catalog=catalog,
        search=CatalogSearchService(catalog),
        asset_read=AssetReadService(catalog=repository, reader=LocalFilesystemBlobReader({})),
        document=document,
        wiki=wiki,
        knowledge_query=KnowledgeQueryRouter(
            catalog=repository,
            engines=build_local_query_engines(wiki=wiki),
        ),
        deployment=deployment,
    )
    admin = RestAdminAdapter(
        authoring=object(),
        processing=object(),
        bindings=StaticProcessingBindingResolver({}),
        wiki_compilation=worker,
    )
    return create_platform_app(
        query_adapter=rest,
        admin_adapter=admin,
        mcp_adapter=McpQueryAdapter(rest),
        principal_provider=lambda: principal,
        correlation_provider=lambda: Correlation("phase8-local-platform-process-wiki-compile-shadow"),
    )


def _build_semantic_processing_app(
    repository: SqliteCatalogQueryRepository,
    worker: SemanticDimensionBuildWorker,
    decisions: SemanticDimensionJobDecisionService,
    principal: Principal,
    deployment: DeploymentActivationController,
):
    catalog = CatalogQueryService(repository)
    wiki_provider = LocalPublishedWikiProvider(catalog=repository, asset_paths={})
    document = DocumentRetrievalService(wiki_provider, repository)
    wiki = WikiQueryService(wiki_provider, repository)
    rest = RestQueryAdapter(
        catalog=catalog,
        search=CatalogSearchService(catalog),
        asset_read=AssetReadService(catalog=repository, reader=LocalFilesystemBlobReader({})),
        document=document,
        wiki=wiki,
        knowledge_query=KnowledgeQueryRouter(catalog=repository, engines=build_local_query_engines(wiki=wiki)),
        deployment=deployment,
    )
    admin = RestAdminAdapter(
        authoring=object(),
        processing=object(),
        bindings=StaticProcessingBindingResolver({}),
        semantic_decisions=decisions,
        semantic_processing=worker,
    )
    return create_platform_app(
        query_adapter=rest,
        admin_adapter=admin,
        mcp_adapter=McpQueryAdapter(rest),
        principal_provider=lambda: principal,
        correlation_provider=lambda: Correlation("phase8-local-platform-process-semantic-shadow"),
    )


def _build_capture_processing_app(
    repository: SqliteCatalogQueryRepository,
    worker: CaptureProcessingWorker,
    principal: Principal,
    deployment: DeploymentActivationController,
):
    catalog = CatalogQueryService(repository)
    wiki_provider = LocalPublishedWikiProvider(catalog=repository, asset_paths={})
    document = DocumentRetrievalService(wiki_provider, repository)
    wiki = WikiQueryService(wiki_provider, repository)
    rest = RestQueryAdapter(
        catalog=catalog,
        search=CatalogSearchService(catalog),
        asset_read=AssetReadService(catalog=repository, reader=LocalFilesystemBlobReader({})),
        document=document,
        wiki=wiki,
        knowledge_query=KnowledgeQueryRouter(catalog=repository, engines=build_local_query_engines(wiki=wiki)),
        deployment=deployment,
    )
    admin = RestAdminAdapter(
        authoring=object(),
        processing=object(),
        bindings=StaticProcessingBindingResolver({}),
        capture_processing=worker,
    )
    return create_platform_app(
        query_adapter=rest,
        admin_adapter=admin,
        mcp_adapter=McpQueryAdapter(rest),
        principal_provider=lambda: principal,
        correlation_provider=lambda: Correlation("phase8-local-platform-process-capture-shadow"),
    )


def _build_connector_sync_app(
    repository: SqliteCatalogQueryRepository,
    worker: ConnectorSyncWorker,
    principal: Principal,
    deployment: DeploymentActivationController,
    *,
    source_paths: dict[str, Path],
    source_digests: dict[str, str],
):
    catalog = CatalogQueryService(repository)
    wiki_provider = LocalPublishedWikiProvider(catalog=repository, asset_paths={})
    document = DocumentRetrievalService(wiki_provider, repository)
    wiki = WikiQueryService(wiki_provider, repository)
    rest = RestQueryAdapter(
        catalog=catalog,
        search=CatalogSearchService(catalog),
        asset_read=AssetReadService(catalog=repository, reader=LocalFilesystemBlobReader({})),
        document=document,
        wiki=wiki,
        knowledge_query=KnowledgeQueryRouter(catalog=repository, engines=build_local_query_engines(wiki=wiki)),
        deployment=deployment,
    )
    admin = RestAdminAdapter(
        authoring=object(),
        processing=object(),
        bindings=StaticProcessingBindingResolver({}),
        connector_sync=worker,
        connector_sync_paths=source_paths,
        connector_sync_digests=source_digests,
    )
    return create_platform_app(
        query_adapter=rest,
        admin_adapter=admin,
        mcp_adapter=McpQueryAdapter(rest),
        principal_provider=lambda: principal,
        correlation_provider=lambda: Correlation("phase8-local-platform-process-connector-sync-shadow"),
    )


def _build_gbrain_projection_app(
    repository: SqliteCatalogQueryRepository,
    projector: LocalGbrainProjectionService,
    principal: Principal,
    deployment: DeploymentActivationController,
    *,
    binding: GbrainProjectionBinding,
):
    catalog = CatalogQueryService(repository)
    wiki_provider = LocalPublishedWikiProvider(catalog=repository, asset_paths={})
    document = DocumentRetrievalService(wiki_provider, repository)
    wiki = WikiQueryService(wiki_provider, repository)
    rest = RestQueryAdapter(
        catalog=catalog,
        search=CatalogSearchService(catalog),
        asset_read=AssetReadService(catalog=repository, reader=LocalFilesystemBlobReader({})),
        document=document,
        wiki=wiki,
        knowledge_query=KnowledgeQueryRouter(catalog=repository, engines=build_local_query_engines(wiki=wiki)),
        deployment=deployment,
    )
    admin = RestAdminAdapter(
        authoring=object(),
        processing=object(),
        bindings=StaticProcessingBindingResolver({}),
        gbrain_projection=projector,
        gbrain_projection_bindings={binding.asset_id: binding},
    )
    return create_platform_app(
        query_adapter=rest,
        admin_adapter=admin,
        mcp_adapter=McpQueryAdapter(rest),
        principal_provider=lambda: principal,
        correlation_provider=lambda: Correlation("phase8-local-platform-process-gbrain-shadow"),
    )


def _build_database_app(
    repository: SqliteCatalogQueryRepository,
    nl2sql: DatabaseNl2SqlService,
    execute: DatabaseExecuteReadonlyService,
    schema: DatabaseSchemaQueryService,
    principal: Principal,
    deployment: DeploymentActivationController,
):
    catalog = CatalogQueryService(repository)
    wiki_provider = LocalPublishedWikiProvider(catalog=repository, asset_paths={})
    document = DocumentRetrievalService(wiki_provider, repository)
    wiki = WikiQueryService(wiki_provider, repository)
    rest = RestQueryAdapter(
        catalog=catalog,
        search=CatalogSearchService(catalog),
        asset_read=AssetReadService(catalog=repository, reader=LocalFilesystemBlobReader({})),
        document=document,
        wiki=wiki,
        database_nl2sql=nl2sql,
        database_execute=execute,
        database_schema=schema,
        knowledge_query=KnowledgeQueryRouter(
            catalog=repository,
            engines=build_local_query_engines(database_nl2sql=nl2sql),
        ),
        deployment=deployment,
    )
    return create_platform_app(
        query_adapter=rest,
        mcp_adapter=McpQueryAdapter(rest),
        principal_provider=lambda: principal,
        correlation_provider=lambda: Correlation("phase8-local-platform-process-database-shadow"),
    )


def run_server(
    *,
    catalog: Path,
    wiki_root: Path,
    temp_dir: Path,
    port: int,
    ready_file: Path,
    asset_binding_manifest: Path | None = None,
    lexical_manifest: Path | None = None,
    dense_manifest: Path | None = None,
    vector_uri: str = "http://127.0.0.1:19530",
    embedding_model_dir: Path | None = None,
    embedding_dimension: int = 2048,
    embedding_max_length: int = 1024,
    table_asset_id: str | None = None,
    table_file: Path | None = None,
    table_sheet: str | int | None = None,
    database_mode: bool = False,
    database_host: str = "127.0.0.1",
    database_port: int = 5432,
    database_name: str = "insight_data",
    database_user: str = "pet",
    database_password: str = "",
    database_vanna_collection: Path | None = None,
    database_vanna_collection_name: str | None = None,
    database_vanna_package_revision: str | None = None,
    database_vanna_input_digest: str | None = None,
    logical_processing_mode: bool = False,
    logical_source_file: Path | None = None,
    logical_dataset_id: str = _LOGICAL_DATASET_ID,
    semantic_dimension_mode: bool = False,
    semantic_job_id: str | None = None,
    semantic_space_id: str = _SPACE_ID,
    capture_processing_mode: bool = False,
    capture_asset_id: str | None = None,
    capture_file: Path | None = None,
    connector_sync_mode: bool = False,
    connector_id: str | None = None,
    connector_source_item_id: str | None = None,
    connector_file: Path | None = None,
    gbrain_projection_mode: bool = False,
    gbrain_asset_id: str | None = None,
    gbrain_file: Path | None = None,
    wiki_compile_mode: bool = False,
    wiki_compile_asset_id: str | None = None,
    wiki_compile_file: Path | None = None,
    semantic_markdown_db: Path | None = None,
    upload_bindings: Mapping[str, Path] | None = None,
    package_bindings: Mapping[str, Path] | None = None,
    index_source_bindings: Mapping[str, Path] | None = None,
    index_provider_id: str = "puddingclaw_platform_candidate_text",
    index_capability: str = "document_rag_query",
    console_origin: str | None = None,
    asset_binding_review_queue: Path | None = None,
) -> None:
    try:
        # macOS commonly exposes /tmp as a symlink to /private/tmp.  The
        # deployment state store intentionally rejects symlink-containing
        # paths, so resolve the explicitly supplied shadow workspace before
        # creating any local state beneath it.
        temp_dir = temp_dir.expanduser().resolve()
        ready_file = ready_file.expanduser().resolve()
        temp_dir.mkdir(parents=True, exist_ok=True)
        temporary_catalog = temp_dir / "knowledge-platform.sqlite3"
        if table_asset_id is not None or database_mode or logical_processing_mode or semantic_dimension_mode or capture_processing_mode or connector_sync_mode or gbrain_projection_mode or wiki_compile_mode:
            shutil.copy2(catalog, temporary_catalog)
            materialized = {"pages": 0, "file_bindings": {}}
        else:
            materialized = _materialize_catalog(catalog, temporary_catalog, wiki_root)
        repository = SqliteCatalogQueryRepository(temporary_catalog)
        selected_modes = sum(
            value is not None for value in (lexical_manifest, dense_manifest, table_asset_id)
        ) + int(database_mode) + int(logical_processing_mode) + int(semantic_dimension_mode) + int(capture_processing_mode) + int(connector_sync_mode) + int(gbrain_projection_mode) + int(wiki_compile_mode)
        if selected_modes > 1:
            raise ValueError("provider and Processing/Authoring sidecar modes are mutually exclusive")
        explicit_asset_bindings: dict[str, Path] = {}
        if asset_binding_manifest is not None:
            if selected_modes > 0:
                raise ValueError("Asset binding manifest is supported only by the default local query process")
            from scripts.phase9_local_asset_binding_prepare import load_binding_manifest

            explicit_asset_bindings = load_binding_manifest(
                manifest_path=asset_binding_manifest,
                catalog_path=catalog,
                space_id=_SPACE_ID,
            )
            materialized["file_bindings"] = _merge_explicit_asset_bindings(
                materialized["file_bindings"], explicit_asset_bindings
            )
        if (semantic_markdown_db is not None or upload_bindings or package_bindings or index_source_bindings or asset_binding_review_queue is not None) and selected_modes > 0:
            raise ValueError("Admin staging bindings are supported only by the default local query process")
        if database_mode and (table_asset_id is not None or table_file is not None or lexical_manifest is not None or dense_manifest is not None):
            raise ValueError("database sidecar mode cannot include another provider mode")
        if database_mode and database_vanna_collection is not None and (
            not database_vanna_collection_name
            or not database_vanna_package_revision
            or not database_vanna_input_digest
        ):
            raise ValueError("local database Vanna Collection requires an explicit identity binding")
        if logical_processing_mode and (
            table_asset_id is not None
            or table_file is not None
            or lexical_manifest is not None
            or dense_manifest is not None
            or database_mode
            or logical_source_file is None
        ):
            raise ValueError("logical Processing mode requires only an explicit source file")
        if semantic_dimension_mode and (
            semantic_job_id is None
            or not semantic_job_id.strip()
            or not semantic_space_id.strip()
            or table_asset_id is not None
            or table_file is not None
            or lexical_manifest is not None
            or dense_manifest is not None
            or database_mode
            or logical_processing_mode
        ):
            raise ValueError("semantic Processing mode requires only an explicit job and Space")
        if capture_processing_mode and (
            capture_asset_id is None
            or not capture_asset_id.strip()
            or capture_file is None
            or table_asset_id is not None
            or table_file is not None
            or lexical_manifest is not None
            or dense_manifest is not None
            or database_mode
            or logical_processing_mode
            or semantic_dimension_mode
        ):
            raise ValueError("Capture Processing mode requires only an explicit Asset and source file")
        if connector_sync_mode and (
            connector_id is None
            or not connector_id.strip()
            or connector_source_item_id is None
            or not connector_source_item_id.strip()
            or connector_file is None
            or table_asset_id is not None
            or table_file is not None
            or lexical_manifest is not None
            or dense_manifest is not None
            or database_mode
            or logical_processing_mode
            or semantic_dimension_mode
            or capture_processing_mode
        ):
            raise ValueError("Connector Sync mode requires only an explicit connector, source item and file")
        if gbrain_projection_mode and (
            gbrain_asset_id is None
            or not gbrain_asset_id.strip()
            or gbrain_file is None
            or table_asset_id is not None
            or table_file is not None
            or lexical_manifest is not None
            or dense_manifest is not None
            or database_mode
            or logical_processing_mode
            or semantic_dimension_mode
            or capture_processing_mode
            or connector_sync_mode
        ):
            raise ValueError("gbrain projection mode requires only an explicit Asset and file")
        if wiki_compile_mode and (
            wiki_compile_asset_id is None
            or wiki_compile_file is None
            or table_asset_id is not None
            or table_file is not None
            or lexical_manifest is not None
            or dense_manifest is not None
            or database_mode
            or logical_processing_mode
        ):
            raise ValueError("Wiki compile mode requires only an explicit Asset and source file")
        if (table_asset_id is None) != (table_file is None):
            raise ValueError("table sidecar mode requires both table asset and explicit file")
        provider_id = "local-wiki"
        if lexical_manifest is not None:
            provider_id = _PROVIDER_ID
        elif dense_manifest is not None:
            provider_id = _DENSE_PROVIDER_ID
        elif table_asset_id is not None:
            provider_id = "local-table-" + table_asset_id
        elif database_mode:
            provider_id = _DATABASE_DATASET_ID
        elif logical_processing_mode:
            provider_id = "local-logical-" + logical_dataset_id
        elif semantic_dimension_mode:
            provider_id = "local-semantic-" + semantic_job_id
        elif capture_processing_mode:
            provider_id = "local-capture-" + capture_asset_id
        elif connector_sync_mode:
            provider_id = "local-connector-" + connector_id
        elif gbrain_projection_mode:
            provider_id = "local-gbrain-" + gbrain_asset_id
        elif wiki_compile_mode:
            provider_id = "local-wiki-compile-" + wiki_compile_asset_id
        deployment = _deployment_controller(
            temporary_catalog=temporary_catalog,
            temp_dir=temp_dir,
            provider_id=provider_id,
        )
        principal = Principal(
            subject_id="phase8-local-platform-process-shadow",
            scopes=(
                "knowledge.list",
                "knowledge.read",
                "knowledge.query",
                "knowledge.search",
                "knowledge.processing",
                *(('knowledge.admin',) if semantic_markdown_db is not None or upload_bindings or package_bindings or index_source_bindings or asset_binding_review_queue is not None else ()),
                "knowledge.space:space_kb_default",
            ),
        )
        semantic_markdown = (
            SemanticMarkdownAdminService(
                repository=SqliteSemanticMarkdownRepository(semantic_markdown_db.expanduser().absolute())
            )
            if semantic_markdown_db is not None
            else None
        )
        capability = "wiki_query"
        binding_present = False
        if gbrain_projection_mode:
            source_file = gbrain_file.expanduser().absolute() if gbrain_file is not None else None
            if gbrain_asset_id is None or source_file is None or not source_file.is_file():
                raise ValueError("gbrain projection source file is unavailable")
            asset = repository.get_asset(asset_id=gbrain_asset_id)
            if asset is None or str(asset.get("space_id") or "") != _SPACE_ID:
                raise ValueError("gbrain projection Asset is unavailable")
            source_digest = _file_digest(source_file)
            if source_digest != str(asset.get("content_digest") or "") or source_digest != str(asset.get("revision") or ""):
                raise ValueError("gbrain projection source digest does not match Catalog")
            try:
                markdown = source_file.read_text(encoding="utf-8")
            except UnicodeDecodeError as error:
                raise ValueError("gbrain projection source is not UTF-8") from error
            gbrain_binding = GbrainProjectionBinding(
                space_id=_SPACE_ID,
                asset_id=gbrain_asset_id,
                source_uri=str(asset.get("source_uri") or ""),
                published_uri=f"knowledge://spaces/{_SPACE_ID}/wiki/{gbrain_asset_id}",
                source_revision=str(asset.get("revision") or ""),
                published_digest=source_digest,
                published_markdown=markdown,
            )
            gbrain_principal = Principal(
                subject_id="phase8-local-platform-process-gbrain-shadow",
                scopes=(
                    "knowledge.query",
                    "knowledge.search",
                    "knowledge.admin",
                    "knowledge.processing",
                    f"knowledge.space:{_SPACE_ID}",
                ),
            )
            capability = "gbrain_projection"
            binding_present = True
            app = _build_gbrain_projection_app(
                repository,
                LocalGbrainProjectionService(root=temp_dir / "gbrain-projection"),
                gbrain_principal,
                deployment,
                binding=gbrain_binding,
            )
        elif connector_sync_mode:
            source_file = connector_file.expanduser().absolute() if connector_file is not None else None
            if connector_id is None or connector_source_item_id is None or source_file is None or not source_file.is_file():
                raise ValueError("Connector Sync source file is unavailable")
            with sqlite3.connect(temporary_catalog) as connection:
                connector = connection.execute(
                    "SELECT space_id, status FROM knowledge_connectors WHERE id = ?",
                    (connector_id,),
                ).fetchone()
                source_item = connection.execute(
                    "SELECT space_id, connector_id, status, content_digest FROM knowledge_source_items WHERE id = ?",
                    (connector_source_item_id,),
                ).fetchone()
            if (
                connector is None
                or connector[0] != _SPACE_ID
                or connector[1] not in {"ready", "active"}
                or source_item is None
                or source_item[0] != _SPACE_ID
                or source_item[1] != connector_id
                or source_item[2] != "ready"
                or not source_item[3]
            ):
                raise ValueError("Connector Sync binding is unavailable")
            if _file_digest(source_file) != source_item[3]:
                raise ValueError("Connector Sync source digest does not match Catalog")
            connector_principal = Principal(
                subject_id="phase8-local-platform-process-connector-sync-shadow",
                scopes=(
                    "knowledge.query",
                    "knowledge.search",
                    "knowledge.admin",
                    "knowledge.processing",
                    f"knowledge.space:{_SPACE_ID}",
                ),
            )
            connector_worker = ConnectorSyncWorker(
                source=LocalConnectorSourceProvider(
                    expected_digests={connector_source_item_id: str(source_item[3])}
                ),
                store=SqliteConnectorSyncStore(database_path=temporary_catalog),
            )
            capability = "connector_sync"
            binding_present = True
            app = _build_connector_sync_app(
                repository,
                connector_worker,
                connector_principal,
                deployment,
                source_paths={connector_source_item_id: source_file},
                source_digests={connector_source_item_id: str(source_item[3])},
            )
        elif capture_processing_mode:
            source_file = capture_file.expanduser().absolute() if capture_file is not None else None
            if capture_asset_id is None or source_file is None or not source_file.is_file():
                raise ValueError("Capture source file is unavailable")
            asset = repository.get_asset(asset_id=capture_asset_id)
            if asset is None or str(asset.get("space_id") or "") != _SPACE_ID:
                raise ValueError("Capture Asset is unavailable")
            capture_principal = Principal(
                subject_id="phase8-local-platform-process-capture-shadow",
                scopes=(
                    "knowledge.query",
                    "knowledge.search",
                    "knowledge.admin",
                    "knowledge.processing",
                    f"knowledge.space:{_SPACE_ID}",
                ),
            )
            snapshot = LocalImmutableRawSnapshotRepository(
                snapshot_root=temp_dir / "capture-raw",
                snapshot_id=capture_asset_id,
                source_revision=str(asset.get("revision") or ""),
                source_uri=str(asset.get("source_uri") or ""),
                path=source_file,
                expected_digest=str(asset.get("content_digest") or ""),
            )
            capture_worker = CaptureProcessingWorker(
                snapshots=snapshot,
                publisher=LocalCapturePublishingService(root=temp_dir / "capture-published", space_id=_SPACE_ID),
                jobs=SqliteCaptureProcessingJobStore(database_path=temporary_catalog, space_id=_SPACE_ID),
            )
            capability = "capture_processing"
            binding_present = True
            app = _build_capture_processing_app(repository, capture_worker, capture_principal, deployment)
        elif semantic_dimension_mode:
            semantic_principal = Principal(
                subject_id="phase8-local-platform-process-semantic-shadow",
                scopes=(
                    "knowledge.query",
                    "knowledge.search",
                    "knowledge.admin",
                    "knowledge.processing",
                    f"knowledge.space:{semantic_space_id}",
                ),
            )
            semantic_worker = SemanticDimensionBuildWorker(
                builder=LocalSemanticDimensionBuilder(),
                jobs=SqliteSemanticDimensionJobStore(database_path=temporary_catalog),
                publisher=LocalSemanticDimensionPublisher(root=temp_dir / "semantic-publication"),
            )
            semantic_decisions = SemanticDimensionJobDecisionService(
                writer=SqliteSemanticDimensionJobWriter(temporary_catalog)
            )
            with sqlite3.connect(temporary_catalog) as connection:
                job = connection.execute(
                    "SELECT kind, scope_uri FROM knowledge_authoring_jobs WHERE id = ?",
                    (semantic_job_id,),
                ).fetchone()
            if (
                job is None
                or job[0] != "semantic_dimension_build"
                or job[1] != f"knowledge://spaces/{semantic_space_id}/semantic-dimensions/{_SEMANTIC_DIMENSION_ID}"
            ):
                raise ValueError("semantic Processing job is unavailable")
            capability = "semantic_dimension_processing"
            binding_present = True
            app = _build_semantic_processing_app(
                repository,
                semantic_worker,
                semantic_decisions,
                semantic_principal,
                deployment,
            )
        elif wiki_compile_mode:
            compile_file = wiki_compile_file.expanduser().absolute() if wiki_compile_file is not None else None
            if compile_file is None or wiki_compile_asset_id is None or not compile_file.is_file():
                raise ValueError("Wiki compile source file is unavailable")
            assets = {
                str(item.get("id") or ""): item
                for item in repository.list_assets(space_id=_SPACE_ID)
                if isinstance(item, dict)
            }
            asset = assets.get(wiki_compile_asset_id)
            if asset is None or str(asset.get("kind") or "") != "document":
                raise ValueError("Wiki compile Asset is unavailable")
            space_id = str(asset.get("space_id") or "")
            if space_id != _SPACE_ID:
                raise ValueError("Wiki compile Asset is outside the local Space")
            snapshot = LocalImmutableRawSnapshotRepository(
                snapshot_root=temp_dir / "raw",
                snapshot_id=wiki_compile_asset_id,
                source_revision=str(asset.get("revision") or ""),
                source_uri=str(asset.get("source_uri") or ""),
                path=compile_file,
                expected_digest=str(asset.get("content_digest") or ""),
            )
            publisher = LocalWikiPublishingService(root=temp_dir / "wiki-published", space_id=_SPACE_ID)
            worker = WikiCompilationWorker(
                snapshots=snapshot,
                context=BoundedWikiContextService(),
                model=DeterministicWikiModelGateway({wiki_compile_asset_id: str(asset.get("title") or "Compiled Wiki")}),
                validator=LocalWikiDraftValidator(),
                publisher=publisher,
                jobs=SqliteWikiCompilationJobStore(
                    database_path=temporary_catalog,
                    space_id=_SPACE_ID,
                ),
            )
            compile_principal = Principal(
                subject_id="phase8-local-platform-process-wiki-compile-shadow",
                scopes=("knowledge.query", "knowledge.search", "knowledge.admin", "knowledge.processing", f"knowledge.space:{_SPACE_ID}"),
            )
            capability = "wiki_compile"
            binding_present = True
            app = _build_wiki_compile_app(
                repository,
                worker,
                wiki_compile_asset_id,
                temp_dir / "wiki-published" / "wiki" / f"{wiki_compile_asset_id}.md",
                compile_principal,
                deployment,
            )
        elif logical_processing_mode:
            logical_file = logical_source_file.expanduser().absolute() if logical_source_file is not None else None
            if logical_file is None or not logical_file.is_file():
                raise ValueError("logical Processing source file is unavailable")
            bounded_source = temp_dir / "logical-bounded-source.csv"
            _bounded_csv(logical_file, bounded_source)
            source_provider = LocalStructuredFileProvider(
                asset_paths={_LOGICAL_SOURCE_ID: bounded_source},
                asset_uris={_LOGICAL_SOURCE_ID: f"knowledge://spaces/{_SPACE_ID}/structured-assets/{_LOGICAL_SOURCE_ID}/source"},
            )
            source_profile = source_provider.inspect_source(path=bounded_source)
            source = repository.get_structured_asset(asset_id=_LOGICAL_SOURCE_ID)
            if source is None:
                _stage_logical_source(temporary_catalog, source_path=bounded_source, profile=source_profile)
                repository = SqliteCatalogQueryRepository(temporary_catalog)
                source = repository.get_structured_asset(asset_id=_LOGICAL_SOURCE_ID)
            if source is None or str(source.get("content_digest") or "") != source_profile.content_digest:
                raise ValueError("logical Processing source digest is not stable")
            logical_principal = Principal(
                subject_id="phase8-local-platform-process-logical-shadow",
                scopes=(
                    "knowledge.query",
                    "knowledge.search",
                    "knowledge.table_query",
                    "knowledge.admin",
                    "knowledge.processing",
                    f"knowledge.space:{_SPACE_ID}",
                ),
            )
            writer = SqliteStructuredAssetWriter(temporary_catalog)
            if str(source.get("reference_status") or "") == "pending":
                bound = writer.bind_source_asset(
                    principal=logical_principal,
                    asset_id=_LOGICAL_SOURCE_ID,
                    space_id=_SPACE_ID,
                    expected_content_digest=source_profile.content_digest,
                    profile=source_profile,
                )
                if str(bound.get("reference_status") or "") not in {"ready", "verified", "active"}:
                    raise ValueError("logical Processing source binding was rejected")
            repository = SqliteCatalogQueryRepository(temporary_catalog)
            source = repository.get_structured_asset(asset_id=_LOGICAL_SOURCE_ID)
            if source is None or str(source.get("reference_status") or "") not in {"ready", "verified", "active"}:
                raise ValueError("logical Processing source is not ready")
            source_uri = str(source["source_uri"])
            dataset_uri = f"knowledge://spaces/{_SPACE_ID}/structured-assets/{logical_dataset_id}/source"
            provider = LocalStructuredFileProvider(
                asset_paths={_LOGICAL_SOURCE_ID: bounded_source},
                logical_asset_sources={logical_dataset_id: ((_LOGICAL_SOURCE_ID, bounded_source),)},
                asset_uris={_LOGICAL_SOURCE_ID: source_uri, logical_dataset_id: dataset_uri},
            )
            table = TableQueryService(provider=provider, catalog=repository)
            collection_binding = writer.bind_collection_provider(
                principal=logical_principal,
                collection_id=_COLLECTION_ID,
                collection_version=_COLLECTION_VERSION,
                space_id=_SPACE_ID,
                capability="table_query",
                binding={"dataset_id": logical_dataset_id},
            )
            if collection_binding.get("binding") != {"dataset_id": logical_dataset_id}:
                raise ValueError("logical Processing Collection binding was rejected")
            authoring = LogicalDatasetAuthoringService(catalog=repository, writer=writer)
            processing = LogicalDatasetProcessingService(catalog=repository, profiler=provider, publisher=writer)
            worker = LogicalDatasetProcessingWorker(
                service=processing,
                jobs=SqliteLogicalDatasetProcessingJobStore(database_path=temporary_catalog),
            )
            bindings = StaticProcessingBindingResolver(
                {logical_dataset_id: {_LOGICAL_SOURCE_ID: bounded_source}},
                space_id=_SPACE_ID,
            )
            capability = "logical_dataset_processing"
            binding_present = True
            app = _build_logical_app(
                repository,
                table,
                authoring,
                processing,
                worker,
                bindings,
                logical_principal,
                deployment,
            )
        elif database_mode:
            database_principal = Principal(
                subject_id="phase8-local-platform-process-database-shadow",
                scopes=(
                    "knowledge.query",
                    "knowledge.database_nl2sql",
                    "knowledge.database_execute_readonly",
                    "knowledge.database_schema",
                    "knowledge.processing",
                    f"knowledge.space:{_SPACE_ID}",
                ),
            )
            source = LocalPostgresDatabaseSource(
                dataset_id=_DATABASE_DATASET_ID,
                space_id=_SPACE_ID,
                host=database_host,
                port=database_port,
                database=database_name,
                username=database_user,
                password=database_password,
                allowed_tables=(_DATABASE_TABLE,),
                dataset_version="local-postgresql-v1",
                deployment_revision="platform-local-process-v1",
                semantic_context_hash="sha256:" + "0" * 64,
            )
            datasets = LocalPostgresDatabaseDatasetResolver([source])
            binding = DatabaseCollectionBindingService(
                datasets=datasets,
                writer=SqliteStructuredAssetWriter(temporary_catalog),
            ).bind(
                principal=database_principal,
                correlation=Correlation("phase8-local-platform-process-database-binding"),
                request=DatabaseCollectionBindingRequest(
                    collection_id=_COLLECTION_ID,
                    collection_version=_COLLECTION_VERSION,
                    space_id=_SPACE_ID,
                    dataset_id=_DATABASE_DATASET_ID,
                ),
            )
            if binding.status != "ok":
                raise ValueError("local database Collection binding was rejected")
            database_binding = datasets.resolve(
                dataset_id=_DATABASE_DATASET_ID,
                space_id=_SPACE_ID,
            )
            if database_binding is None:
                raise ValueError("local database binding could not be resolved")
            repository = SqliteCatalogQueryRepository(temporary_catalog)
            plans = InMemoryQueryPlanRepository()
            validator = PostgresReadonlySqlValidator()
            candidate = DatabaseSqlCandidate(
                sql=(
                    f"SELECT energy_type, COUNT(*) AS model_count FROM {_DATABASE_TABLE} "
                    "GROUP BY energy_type ORDER BY model_count DESC"
                ),
                evidence=_local_database_schema_evidence(source_revision=database_binding.source_revision),
                provider_version="local-postgresql-process-shadow",
            )
            nl2sql = DatabaseNl2SqlService(
                datasets=datasets,
                provider=(
                    GatewayVannaProvider(
                        LocalVannaCollectionGateway(
                            database_vanna_collection,
                            expected_collection_name=database_vanna_collection_name,
                            expected_package_revision=database_vanna_package_revision,
                            expected_input_digest=database_vanna_input_digest,
                        ),
                        version="local-file-backed-collection-shadow",
                    )
                    if database_vanna_collection is not None
                    else StaticNl2SqlProvider({_DATABASE_QUESTION: candidate})
                ),
                validator=validator,
                plans=plans,
            )
            execute = DatabaseExecuteReadonlyService(
                datasets=datasets,
                plans=plans,
                validator=validator,
                executor=PostgresReadonlyDatabaseExecutor(
                    {(_SPACE_ID, _DATABASE_DATASET_ID): source},
                    validator=validator,
                    source_revisions={(_SPACE_ID, _DATABASE_DATASET_ID): database_binding.source_revision},
                ),
            )
            schema = DatabaseSchemaQueryService(
                datasets=datasets,
                reader=LocalPostgresDatabaseSchemaReader({(_SPACE_ID, _DATABASE_DATASET_ID): source}),
            )
            capability = "database_nl2sql"
            collection = next(
                (
                    item
                    for item in repository.list_collections(space_id=_SPACE_ID)
                    if item.get("id") == _COLLECTION_ID and item.get("version") == _COLLECTION_VERSION
                ),
                None,
            )
            binding_present = (
                collection is not None
                and collection.get("provider_bindings", {}).get("database_nl2sql")
                == {"dataset_id": _DATABASE_DATASET_ID}
            )
            app = _build_database_app(repository, nl2sql, execute, schema, database_principal, deployment)
        elif lexical_manifest is None and dense_manifest is None:
            if table_asset_id is None or table_file is None:
                asset_upload = (
                    LocalAssetUploadService(
                        bindings=upload_bindings or {},
                        staging_root=temp_dir / "admin-staging",
                    )
                    if upload_bindings
                    else None
                )
                package_import = (
                    LocalPackageImportService(
                        bindings=package_bindings or {},
                        staging_root=temp_dir / "admin-staging",
                    )
                    if package_bindings
                    else None
                )
                index_rebuild = (
                    CatalogIndexRebuildService(
                        repository,
                        source_bindings=index_source_bindings or {},
                        output_root=temp_dir / "index-staging",
                        provider_id=index_provider_id,
                        capability=index_capability,
                    )
                    if index_source_bindings
                    else None
                )
                connector_authorization = (
                    ConnectorAuthorizationService(
                        repository,
                        staging_root=temp_dir / "authorization-staging",
                    )
                    if semantic_markdown is not None or upload_bindings or package_bindings or index_source_bindings
                    else None
                )
                review_queue = (
                    LocalAssetBindingReviewQueue(asset_binding_review_queue)
                    if asset_binding_review_queue is not None
                    else None
                )
                notification_store = None
                if _has_table(temporary_catalog, "knowledge_notification_event_scopes"):
                    notification_store = SqliteNotificationEventScopeStore(temporary_catalog)
                notifications = NotificationEventQueryService(notification_store) if notification_store is not None else None
                app = _build_app(
                    repository,
                    materialized["file_bindings"],
                    principal,
                    deployment=deployment,
                    semantic_markdown=semantic_markdown,
                    connector_catalog=CatalogConnectorQueryService(repository) if connector_authorization is not None else None,
                    asset_upload=asset_upload,
                    package_import=package_import,
                    index_rebuild=index_rebuild,
                    connector_authorization=connector_authorization,
                    notifications=notifications,
                    asset_binding_review_queue=review_queue,
                )
            else:
                table_principal = Principal(
                    subject_id="phase8-local-platform-process-table-shadow",
                    scopes=(
                        "knowledge.query",
                        "knowledge.search",
                        "knowledge.table_query",
                        "knowledge.processing",
                        f"knowledge.space:{_TABLE_SPACE_ID}",
                    ),
                )
                bind_result = StructuredAssetBindingService(
                    catalog=repository,
                    verifier=LocalStructuredFileBindingVerifier(),
                    writer=SqliteStructuredAssetWriter(temporary_catalog),
                ).bind(
                    principal=table_principal,
                    correlation=Correlation("phase8-local-platform-process-table-binding"),
                    request=StructuredAssetBindingRequest(
                        asset_id=table_asset_id,
                        space_id=_TABLE_SPACE_ID,
                        path=table_file.expanduser().absolute(),
                    ),
                )
                if bind_result.status != "ok":
                    raise ValueError("explicit table binding was not accepted")
                collection_binding = SqliteStructuredAssetWriter(temporary_catalog).bind_collection_provider(
                    principal=table_principal,
                    collection_id=_TABLE_COLLECTION_ID,
                    collection_version=_COLLECTION_VERSION,
                    space_id=_TABLE_SPACE_ID,
                    capability="table_query",
                    binding={"asset_id": table_asset_id},
                )
                repository = SqliteCatalogQueryRepository(temporary_catalog)
                bound_asset = repository.get_structured_asset(asset_id=table_asset_id)
                if bound_asset is None:
                    raise ValueError("bound Structured Asset disappeared")
                provider = LocalStructuredFileProvider(
                    asset_paths={table_asset_id: table_file.expanduser().absolute()},
                    asset_uris={table_asset_id: str(bound_asset["source_uri"])},
                    asset_sheets={table_asset_id: table_sheet},
                )
                table = TableQueryService(provider=provider, catalog=repository)
                collection = next(
                    (
                        item
                        for item in repository.list_collections(space_id=_TABLE_SPACE_ID)
                        if item.get("id") == _TABLE_COLLECTION_ID
                        and item.get("version") == _COLLECTION_VERSION
                    ),
                    None,
                )
                binding_present = (
                    collection is not None
                    and "table_query" in collection.get("capabilities", [])
                    and collection.get("provider_bindings", {}).get("table_query") == {"asset_id": table_asset_id}
                    and collection_binding.get("binding") == {"asset_id": table_asset_id}
                )
                capability = "table_query"
                app = _build_table_app(repository, table, table_principal, deployment)
        else:
            manifest_path = lexical_manifest if lexical_manifest is not None else dense_manifest
            assert manifest_path is not None
            if dense_manifest is not None:
                manifest_path = _validate_dense_manifest_path(manifest_path)
                _validate_dense_loopback_uri(vector_uri)
            manifest = VectorRebuildManifest.from_dict(
                json.loads(manifest_path.expanduser().read_text(encoding="utf-8"))
            )
            from pymilvus import MilvusClient

            client = MilvusClient(uri=vector_uri, timeout=3)
            if lexical_manifest is not None:
                if manifest.provider_collection_name != _PROVIDER_ID:
                    raise ValueError("lexical manifest provider does not match the fixed candidate")
                provider = MilvusBm25CatalogRetrievalProvider(
                    catalog=SqliteCatalogQueryRepository(temporary_catalog),
                    client=client,
                    collection_name=_PROVIDER_ID,
                    manifest=manifest,
                )
                provider_app_builder = _build_lexical_app
            else:
                if manifest.provider_collection_name != _DENSE_PROVIDER_ID:
                    raise ValueError("dense manifest provider does not match the fixed candidate")
                if embedding_model_dir is None:
                    raise ValueError("dense process mode requires an explicit local embedding model directory")
                embedding = LocalTransformersEmbeddingClient(
                    model_dir=embedding_model_dir,
                    dimension=embedding_dimension,
                    batch_size=1,
                    max_length=embedding_max_length,
                )
                provider = MilvusCatalogRetrievalProvider(
                    catalog=SqliteCatalogQueryRepository(temporary_catalog),
                    client=client,
                    collection_name=_DENSE_PROVIDER_ID,
                    manifest=manifest,
                    encode_query=lambda query: embedding.embed((query,))[0],
                )
                provider_app_builder = _build_dense_app
            SqliteStructuredAssetWriter(temporary_catalog).bind_collection_provider(
                principal=Principal(
                    subject_id="phase8-local-platform-process-admin",
                    scopes=("knowledge.processing", f"knowledge.space:{_SPACE_ID}"),
                ),
                collection_id=_COLLECTION_ID,
                collection_version=_COLLECTION_VERSION,
                space_id=_SPACE_ID,
                capability="document_rag_query",
                binding={"provider_id": provider_id},
            )
            repository = SqliteCatalogQueryRepository(temporary_catalog)
            if lexical_manifest is not None:
                provider = MilvusBm25CatalogRetrievalProvider(
                    catalog=repository,
                    client=client,
                    collection_name=_PROVIDER_ID,
                    manifest=manifest,
                )
            else:
                provider = MilvusCatalogRetrievalProvider(
                    catalog=repository,
                    client=client,
                    collection_name=_DENSE_PROVIDER_ID,
                    manifest=manifest,
                    encode_query=lambda query: embedding.embed((query,))[0],
                )
            collection = next(
                (
                    item
                    for item in repository.list_collections(space_id=_SPACE_ID)
                    if item.get("id") == _COLLECTION_ID and item.get("version") == _COLLECTION_VERSION
                ),
                None,
            )
            binding_present = (
                collection is not None
                and collection.get("provider_bindings", {}).get("document_rag_query") == {"provider_id": provider_id}
            )
            capability = "document_rag_query"
            app = provider_app_builder(repository, provider, principal, deployment)
        if console_origin is not None:
            _configure_console_cors(app, console_origin)
        ready_file.write_text(
            json.dumps(
                {
                    "status": "ready",
                    "pages": materialized["pages"],
                    "capability": capability,
                    "provider_id": provider_id,
                    "database_vanna_collection_bound": database_mode and database_vanna_collection is not None,
                    "binding_present": binding_present,
                    "asset_binding_manifest_loaded": asset_binding_manifest is not None,
                    "deployment_revision": deployment.state.active_deployment_revision,
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="error", lifespan="off")
    except Exception as error:  # readiness file is only a bounded status signal
        ready_file.write_text(json.dumps({"status": "failed", "error_type": type(error).__name__}) + "\n", encoding="utf-8")
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--wiki-root", type=Path, required=True)
    parser.add_argument("--temp-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--ready-file", type=Path, required=True)
    parser.add_argument(
        "--asset-binding-manifest",
        type=Path,
        help="explicit inactive Host Asset binding manifest; paths remain host-local and are revalidated at startup",
    )
    parser.add_argument("--lexical-manifest", type=Path)
    parser.add_argument("--dense-manifest", type=Path)
    parser.add_argument("--vector-uri", default="http://127.0.0.1:19530")
    parser.add_argument("--embedding-model-dir", type=Path)
    parser.add_argument("--embedding-dimension", type=int, default=2048)
    parser.add_argument("--embedding-max-length", type=int, default=1024)
    parser.add_argument("--table-asset-id")
    parser.add_argument("--table-file", type=Path)
    parser.add_argument("--table-sheet")
    parser.add_argument("--database-mode", action="store_true")
    parser.add_argument("--db-host", default="127.0.0.1")
    parser.add_argument("--db-port", type=int, default=5432)
    parser.add_argument("--db-name", default="insight_data")
    parser.add_argument("--db-user", default="pet")
    parser.add_argument("--db-password-env", default="PUDDINGCLAW_CANONICAL_DB_PASSWORD")
    parser.add_argument(
        "--database-vanna-collection",
        type=Path,
        help="optional explicit local inactive Vanna Collection candidate used by database shadow mode",
    )
    parser.add_argument(
        "--database-vanna-collection-name",
        help="expected canonical Collection identity for the local database shadow binding",
    )
    parser.add_argument(
        "--database-vanna-package-revision",
        help="expected validated Package revision for the local database shadow binding",
    )
    parser.add_argument(
        "--database-vanna-input-digest",
        help="expected validated Package input digest for the local database shadow binding",
    )
    parser.add_argument("--logical-processing-mode", action="store_true")
    parser.add_argument("--logical-source-file", type=Path)
    parser.add_argument("--logical-dataset-id", default=_LOGICAL_DATASET_ID)
    parser.add_argument("--semantic-dimension-mode", action="store_true")
    parser.add_argument("--semantic-job-id")
    parser.add_argument("--semantic-space-id", default=_SPACE_ID)
    parser.add_argument("--capture-processing-mode", action="store_true")
    parser.add_argument("--capture-asset-id")
    parser.add_argument("--capture-file", type=Path)
    parser.add_argument("--connector-sync-mode", action="store_true")
    parser.add_argument("--connector-id")
    parser.add_argument("--connector-source-item-id")
    parser.add_argument("--connector-file", type=Path)
    parser.add_argument("--gbrain-projection-mode", action="store_true")
    parser.add_argument("--gbrain-asset-id")
    parser.add_argument("--gbrain-file", type=Path)
    parser.add_argument("--wiki-compile-mode", action="store_true")
    parser.add_argument("--wiki-compile-asset-id")
    parser.add_argument("--wiki-compile-file", type=Path)
    parser.add_argument("--semantic-markdown-db", type=Path)
    parser.add_argument(
        "--upload-binding",
        action="append",
        default=[],
        metavar="BINDING_ID=FILE",
        help="explicit local upload binding; the FILE path stays host-local and is never sent over HTTP",
    )
    parser.add_argument(
        "--package-binding",
        action="append",
        default=[],
        metavar="PACKAGE_REF=ZIP",
        help="explicit local Package ZIP binding; the ZIP path stays host-local and is never sent over HTTP",
    )
    parser.add_argument(
        "--index-source",
        action="append",
        default=[],
        metavar="ASSET_ID=FILE",
        help="explicit local Index source binding; the FILE path stays host-local and is never sent over HTTP",
    )
    parser.add_argument("--index-provider-id", default="puddingclaw_platform_candidate_text")
    parser.add_argument("--index-capability", default="document_rag_query")
    parser.add_argument(
        "--console-origin",
        help="optional explicit http loopback origin for local Console CORS (shadow-only)",
    )
    parser.add_argument(
        "--asset-binding-review-queue",
        type=Path,
        help="optional path-free local Asset binding review queue; read-only and admin-scoped",
    )
    args = parser.parse_args()

    def parse_bindings(values: list[str], label: str) -> dict[str, Path]:
        result: dict[str, Path] = {}
        for value in values:
            binding_id, separator, raw_path = value.partition("=")
            if (
                not separator
                or not binding_id
                or not raw_path
                or not binding_id.replace(".", "").replace("_", "").replace("-", "").isalnum()
                or binding_id in result
            ):
                raise SystemExit(f"{label} must use unique BINDING_ID=FILE values")
            result[binding_id] = Path(raw_path).expanduser().absolute()
        return result

    upload_bindings = parse_bindings(args.upload_binding, "--upload-binding")
    package_bindings = parse_bindings(args.package_binding, "--package-binding")
    index_source_bindings = parse_bindings(args.index_source, "--index-source")
    run_server(
        catalog=args.catalog.expanduser().absolute(),
        wiki_root=args.wiki_root.expanduser().absolute(),
        temp_dir=args.temp_dir.expanduser().absolute(),
        port=args.port,
        ready_file=args.ready_file.expanduser().absolute(),
        asset_binding_manifest=args.asset_binding_manifest.expanduser().absolute()
        if args.asset_binding_manifest
        else None,
        lexical_manifest=args.lexical_manifest.expanduser().absolute() if args.lexical_manifest else None,
        dense_manifest=args.dense_manifest.expanduser().absolute() if args.dense_manifest else None,
        vector_uri=args.vector_uri,
        embedding_model_dir=args.embedding_model_dir.expanduser().absolute() if args.embedding_model_dir else None,
        embedding_dimension=args.embedding_dimension,
        embedding_max_length=args.embedding_max_length,
        table_asset_id=args.table_asset_id,
        table_file=args.table_file.expanduser().absolute() if args.table_file else None,
        table_sheet=args.table_sheet,
        database_mode=args.database_mode,
        database_host=args.db_host,
        database_port=args.db_port,
        database_name=args.db_name,
        database_user=args.db_user,
        database_password=os.getenv(args.db_password_env, ""),
        database_vanna_collection=args.database_vanna_collection.expanduser().absolute()
        if args.database_vanna_collection
        else None,
        database_vanna_collection_name=args.database_vanna_collection_name,
        database_vanna_package_revision=args.database_vanna_package_revision,
        database_vanna_input_digest=args.database_vanna_input_digest,
        logical_processing_mode=args.logical_processing_mode,
        logical_source_file=args.logical_source_file.expanduser().absolute() if args.logical_source_file else None,
        logical_dataset_id=args.logical_dataset_id,
        semantic_dimension_mode=args.semantic_dimension_mode,
        semantic_job_id=args.semantic_job_id,
        semantic_space_id=args.semantic_space_id,
        capture_processing_mode=args.capture_processing_mode,
        capture_asset_id=args.capture_asset_id,
        capture_file=args.capture_file.expanduser().absolute() if args.capture_file else None,
        connector_sync_mode=args.connector_sync_mode,
        connector_id=args.connector_id,
        connector_source_item_id=args.connector_source_item_id,
        connector_file=args.connector_file.expanduser().absolute() if args.connector_file else None,
        gbrain_projection_mode=args.gbrain_projection_mode,
        gbrain_asset_id=args.gbrain_asset_id,
        gbrain_file=args.gbrain_file.expanduser().absolute() if args.gbrain_file else None,
        wiki_compile_mode=args.wiki_compile_mode,
        wiki_compile_asset_id=args.wiki_compile_asset_id,
        wiki_compile_file=args.wiki_compile_file.expanduser().absolute() if args.wiki_compile_file else None,
        semantic_markdown_db=args.semantic_markdown_db.expanduser().absolute() if args.semantic_markdown_db else None,
        upload_bindings=upload_bindings,
        package_bindings=package_bindings,
        index_source_bindings=index_source_bindings,
        index_provider_id=args.index_provider_id,
        index_capability=args.index_capability,
        console_origin=args.console_origin,
        asset_binding_review_queue=args.asset_binding_review_queue.expanduser().absolute()
        if args.asset_binding_review_queue
        else None,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
