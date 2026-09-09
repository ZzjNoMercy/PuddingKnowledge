"""Catalog ownership primitives for the future independent Platform DB."""

from .authoring_job_rehearsal import (
    AUTHORING_JOB_FAILURE_CHECKPOINTS,
    AUTHORING_JOB_SOURCE_TABLES,
    AUTHORING_JOB_TARGET_TABLES,
    DEFAULT_AUTHORING_JOB_FAILURE_PROBES,
    AuthoringJobCatalogRehearsalResult,
    run_authoring_job_catalog_rehearsal_with_rollback_probes,
)
from .connector_rehearsal import (
    CONNECTOR_FAILURE_CHECKPOINTS,
    CONNECTOR_SOURCE_TABLES,
    CONNECTOR_TARGET_TABLES,
    DEFAULT_CONNECTOR_FAILURE_PROBES,
    ConnectorCatalogRehearsalResult,
    run_connector_catalog_rehearsal_with_rollback_probes,
)
from .credential_rehearsal import (
    CREDENTIAL_FAILURE_CHECKPOINTS,
    CREDENTIAL_SOURCE_TABLES,
    CREDENTIAL_TARGET_TABLES,
    DEFAULT_CREDENTIAL_FAILURE_PROBES,
    CredentialCatalogRehearsalResult,
    run_feishu_credential_catalog_rehearsal_with_rollback_probes,
)
from .database_source_rehearsal import (
    DATABASE_SOURCE_FAILURE_CHECKPOINTS,
    DATABASE_SOURCE_SOURCE_TABLES,
    DATABASE_SOURCE_TARGET_TABLES,
    DEFAULT_DATABASE_SOURCE_FAILURE_PROBES,
    DatabaseSourceCatalogRehearsalResult,
    run_database_source_catalog_rehearsal_with_rollback_probes,
)
from .harness_migrations import HARNESS_SCHEMA_VERSIONS, migrate_harness_to_latest
from .harness_models import HarnessSchemaVersion, HarnessWorkerAccessLog
from .metadata import (
    HARNESS_METADATA,
    KNOWLEDGE_METADATA,
    CatalogOwner,
    HarnessBase,
    KnowledgeBase,
    create_harness_session_factory,
    create_knowledge_session_factory,
    create_session_factory,
)
from .migration_orchestrator import (
    DEFAULT_MIGRATION_FAILURE_PROBES,
    MIGRATION_FAILURE_CHECKPOINTS,
    CatalogMigrationManifest,
    CatalogSlice,
    MigrationDrainController,
    MigrationInjectedFailure,
    default_catalog_slices,
    run_catalog_migration_rehearsal,
)
from .migrations import CURRENT_SCHEMA_VERSION, SCHEMA_VERSIONS, migrate_to_latest
from .models import (
    KnowledgeAsset,
    KnowledgeAuthoringEvent,
    KnowledgeAuthoringJob,
    KnowledgeCatalogSchemaVersion,
    KnowledgeCollectionBinding,
    KnowledgeConnector,
    KnowledgeCredential,
    KnowledgeCredentialGrant,
    KnowledgeDatabaseSource,
    KnowledgeDataset,
    KnowledgeIngestionEvent,
    KnowledgeIngestionJob,
    KnowledgeNotificationEvent,
    KnowledgeNotificationEventScope,
    KnowledgeOAuthSession,
    KnowledgeProcessingEvent,
    KnowledgeProcessingJob,
    KnowledgeQueryResult,
    KnowledgeQueryResultScope,
    KnowledgeSourceItem,
    KnowledgeSpace,
    KnowledgeStructuredAsset,
    KnowledgeSyncRun,
    KnowledgeWebCapture,
)
from .notification_event_rehearsal import (
    DEFAULT_NOTIFICATION_FAILURE_PROBES,
    NOTIFICATION_FAILURE_CHECKPOINTS,
    NOTIFICATION_SOURCE_TABLES,
    NOTIFICATION_TARGET_TABLES,
    NotificationEventCatalogRehearsalResult,
    run_notification_event_catalog_rehearsal_with_rollback_probes,
)
from .processing_job_rehearsal import (
    DEFAULT_PROCESSING_JOB_FAILURE_PROBES,
    PROCESSING_JOB_FAILURE_CHECKPOINTS,
    PROCESSING_JOB_SOURCE_TABLES,
    PROCESSING_JOB_TARGET_TABLES,
    ProcessingJobCatalogRehearsalResult,
    run_processing_job_catalog_rehearsal_with_rollback_probes,
)
from .query_result_rehearsal import (
    DEFAULT_QUERY_RESULT_FAILURE_PROBES,
    QUERY_RESULT_FAILURE_CHECKPOINTS,
    QUERY_RESULT_SOURCE_TABLES,
    QUERY_RESULT_TARGET_TABLES,
    QueryResultCatalogRehearsalResult,
    run_query_result_catalog_rehearsal_with_rollback_probes,
)
from .read_later_rehearsal import (
    DEFAULT_READ_LATER_FAILURE_PROBES,
    READ_LATER_FAILURE_CHECKPOINTS,
    READ_LATER_SOURCE_TABLES,
    READ_LATER_TARGET_TABLES,
    ReadLaterCatalogRehearsalResult,
    run_read_later_catalog_rehearsal_with_rollback_probes,
)
from .rehearsal import (
    REQUIRED_CHECKS,
    RehearsalReport,
    RehearsalVerificationError,
    TableSnapshot,
    build_table_snapshot,
)
from .rehearsal_runner import (
    CORE_SOURCE_TABLES,
    CORE_TARGET_TABLES,
    FAILURE_CHECKPOINTS,
    CoreCatalogRehearsalResult,
    RehearsalInjectedFailure,
    run_core_catalog_rehearsal,
    run_core_catalog_rehearsal_with_rollback_probes,
)
from .sqlite_authoring import SqliteStructuredAssetWriter
from .sqlite_semantic_authoring import SqliteSemanticDimensionJobWriter
from .table_asset_rehearsal import (
    DEFAULT_STRUCTURED_ASSET_FAILURE_PROBES,
    STRUCTURED_ASSET_FAILURE_CHECKPOINTS,
    STRUCTURED_ASSET_SOURCE_TABLES,
    STRUCTURED_ASSET_TARGET_TABLES,
    StructuredAssetCatalogRehearsalResult,
    run_structured_asset_catalog_rehearsal_with_rollback_probes,
)
from .vault_rebind_rehearsal import (
    DEFAULT_VAULT_FAILURE_PROBES,
    VAULT_FAILURE_CHECKPOINTS,
    LocalCredentialVaultOperator,
    VaultBinding,
    VaultBindingEvidence,
    VaultProviderStateProof,
    VaultRebindInjectedFailure,
    VaultRebindRehearsalResult,
    VaultRebindVerificationError,
    VaultReferenceOperator,
    run_vault_rebind_rehearsal_with_rollback_probes,
)

__all__ = [
    "HARNESS_METADATA",
    "KNOWLEDGE_METADATA",
    "CatalogOwner",
    "HarnessBase",
    "KnowledgeBase",
    "create_harness_session_factory",
    "create_knowledge_session_factory",
    "create_session_factory",
    "HARNESS_SCHEMA_VERSIONS",
    "migrate_harness_to_latest",
    "HarnessSchemaVersion",
    "HarnessWorkerAccessLog",
    "CURRENT_SCHEMA_VERSION",
    "SCHEMA_VERSIONS",
    "migrate_to_latest",
    "CatalogMigrationManifest",
    "CatalogSlice",
    "MigrationDrainController",
    "MigrationInjectedFailure",
    "default_catalog_slices",
    "MIGRATION_FAILURE_CHECKPOINTS",
    "DEFAULT_MIGRATION_FAILURE_PROBES",
    "run_catalog_migration_rehearsal",
    "KnowledgeAsset",
    "KnowledgeCatalogSchemaVersion",
    "KnowledgeConnector",
    "KnowledgeCollectionBinding",
    "KnowledgeCredential",
    "KnowledgeCredentialGrant",
    "KnowledgeDatabaseSource",
    "KnowledgeDataset",
    "KnowledgeWebCapture",
    "KnowledgeIngestionJob",
    "KnowledgeNotificationEvent",
    "KnowledgeNotificationEventScope",
    "KnowledgeIngestionEvent",
    "KnowledgeStructuredAsset",
    "SqliteStructuredAssetWriter",
    "CollectionFreshnessObservation",
    "CollectionFreshnessWriter",
    "SqliteCollectionFreshnessWriter",
    "CollectionFreshnessObservationService",
    "KnowledgeQueryResult",
    "KnowledgeQueryResultScope",
    "KnowledgeProcessingJob",
    "KnowledgeProcessingEvent",
    "KnowledgeAuthoringJob",
    "KnowledgeAuthoringEvent",
    "KnowledgeOAuthSession",
    "KnowledgeSourceItem",
    "KnowledgeSpace",
    "KnowledgeSyncRun",
    "RehearsalReport",
    "REQUIRED_CHECKS",
    "RehearsalVerificationError",
    "TableSnapshot",
    "build_table_snapshot",
    "CORE_SOURCE_TABLES",
    "CORE_TARGET_TABLES",
    "FAILURE_CHECKPOINTS",
    "CoreCatalogRehearsalResult",
    "RehearsalInjectedFailure",
    "run_core_catalog_rehearsal",
    "run_core_catalog_rehearsal_with_rollback_probes",
    "CONNECTOR_FAILURE_CHECKPOINTS",
    "CONNECTOR_SOURCE_TABLES",
    "CONNECTOR_TARGET_TABLES",
    "DEFAULT_CONNECTOR_FAILURE_PROBES",
    "ConnectorCatalogRehearsalResult",
    "run_connector_catalog_rehearsal_with_rollback_probes",
    "DATABASE_SOURCE_FAILURE_CHECKPOINTS",
    "DATABASE_SOURCE_SOURCE_TABLES",
    "DATABASE_SOURCE_TARGET_TABLES",
    "DEFAULT_DATABASE_SOURCE_FAILURE_PROBES",
    "DatabaseSourceCatalogRehearsalResult",
    "run_database_source_catalog_rehearsal_with_rollback_probes",
    "CREDENTIAL_FAILURE_CHECKPOINTS",
    "CREDENTIAL_SOURCE_TABLES",
    "CREDENTIAL_TARGET_TABLES",
    "DEFAULT_CREDENTIAL_FAILURE_PROBES",
    "CredentialCatalogRehearsalResult",
    "run_feishu_credential_catalog_rehearsal_with_rollback_probes",
    "AUTHORING_JOB_FAILURE_CHECKPOINTS",
    "AUTHORING_JOB_SOURCE_TABLES",
    "AUTHORING_JOB_TARGET_TABLES",
    "DEFAULT_AUTHORING_JOB_FAILURE_PROBES",
    "AuthoringJobCatalogRehearsalResult",
    "run_authoring_job_catalog_rehearsal_with_rollback_probes",
    "VAULT_FAILURE_CHECKPOINTS",
    "DEFAULT_VAULT_FAILURE_PROBES",
    "VaultBinding",
    "VaultBindingEvidence",
    "LocalCredentialVaultOperator",
    "VaultRebindInjectedFailure",
    "VaultRebindRehearsalResult",
    "VaultRebindVerificationError",
    "VaultProviderStateProof",
    "VaultReferenceOperator",
    "run_vault_rebind_rehearsal_with_rollback_probes",
    "NOTIFICATION_FAILURE_CHECKPOINTS",
    "NOTIFICATION_SOURCE_TABLES",
    "NOTIFICATION_TARGET_TABLES",
    "DEFAULT_NOTIFICATION_FAILURE_PROBES",
    "NotificationEventCatalogRehearsalResult",
    "run_notification_event_catalog_rehearsal_with_rollback_probes",
    "READ_LATER_FAILURE_CHECKPOINTS",
    "READ_LATER_SOURCE_TABLES",
    "READ_LATER_TARGET_TABLES",
    "DEFAULT_READ_LATER_FAILURE_PROBES",
    "ReadLaterCatalogRehearsalResult",
    "run_read_later_catalog_rehearsal_with_rollback_probes",
    "STRUCTURED_ASSET_FAILURE_CHECKPOINTS",
    "STRUCTURED_ASSET_SOURCE_TABLES",
    "STRUCTURED_ASSET_TARGET_TABLES",
    "DEFAULT_STRUCTURED_ASSET_FAILURE_PROBES",
    "StructuredAssetCatalogRehearsalResult",
    "run_structured_asset_catalog_rehearsal_with_rollback_probes",
    "SqliteSemanticDimensionJobWriter",
    "SqliteSemanticDimensionJobStore",
    "QUERY_RESULT_FAILURE_CHECKPOINTS",
    "QUERY_RESULT_SOURCE_TABLES",
    "QUERY_RESULT_TARGET_TABLES",
    "DEFAULT_QUERY_RESULT_FAILURE_PROBES",
    "QueryResultCatalogRehearsalResult",
    "run_query_result_catalog_rehearsal_with_rollback_probes",
    "CatalogQueryRepository",
    "CatalogQueryService",
    "SqliteCatalogQueryRepository",
    "SqliteLogicalDatasetProcessingJobStore",
    "PROCESSING_JOB_FAILURE_CHECKPOINTS",
    "PROCESSING_JOB_SOURCE_TABLES",
    "PROCESSING_JOB_TARGET_TABLES",
    "DEFAULT_PROCESSING_JOB_FAILURE_PROBES",
    "ProcessingJobCatalogRehearsalResult",
    "run_processing_job_catalog_rehearsal_with_rollback_probes",
    "CatalogActivationAuditEvent",
    "CatalogActivationController",
    "CatalogActivationError",
    "CatalogActivationState",
    "CatalogActivationStore",
    "SqliteCatalogActivationStore",
    "DeploymentActivationAuditEvent",
    "DeploymentActivationController",
    "DeploymentActivationError",
    "DeploymentActivationState",
    "DeploymentActivationStore",
    "DeploymentArtifact",
    "DeploymentManifest",
    "DeploymentReadContext",
    "SqliteDeploymentActivationStore",
    "LocalProviderHandle",
    "LocalProviderObservationError",
    "deployment_manifest_from_provider_handles",
    "observe_local_provider_handles",
]


def __getattr__(name: str):
    """Load the query slice lazily so baseline probes observe only their scope."""

    if name == "CatalogQueryRepository":
        from .query import CatalogQueryRepository

        return CatalogQueryRepository
    if name == "CatalogQueryService":
        from .service import CatalogQueryService

        return CatalogQueryService
    if name == "SqliteCatalogQueryRepository":
        from .sqlite_query import SqliteCatalogQueryRepository

        return SqliteCatalogQueryRepository
    if name == "SqliteLogicalDatasetProcessingJobStore":
        from .sqlite_processing import SqliteLogicalDatasetProcessingJobStore

        return SqliteLogicalDatasetProcessingJobStore
    if name in {
        "CollectionFreshnessObservation",
        "CollectionFreshnessWriter",
        "SqliteCollectionFreshnessWriter",
        "CollectionFreshnessObservationService",
    }:
        from .sqlite_freshness import (
            CollectionFreshnessObservation,
            CollectionFreshnessObservationService,
            CollectionFreshnessWriter,
            SqliteCollectionFreshnessWriter,
        )

        return {
            "CollectionFreshnessObservation": CollectionFreshnessObservation,
            "CollectionFreshnessWriter": CollectionFreshnessWriter,
            "SqliteCollectionFreshnessWriter": SqliteCollectionFreshnessWriter,
            "CollectionFreshnessObservationService": CollectionFreshnessObservationService,
        }[name]
    if name == "SqliteSemanticDimensionJobStore":
        from .sqlite_semantic_worker import SqliteSemanticDimensionJobStore

        return SqliteSemanticDimensionJobStore
    if name in {
        "CatalogActivationAuditEvent",
        "CatalogActivationController",
        "CatalogActivationError",
        "CatalogActivationState",
        "CatalogActivationStore",
    }:
        from .activation import (
            CatalogActivationAuditEvent,
            CatalogActivationController,
            CatalogActivationError,
            CatalogActivationState,
            CatalogActivationStore,
        )

        return {
            "CatalogActivationAuditEvent": CatalogActivationAuditEvent,
            "CatalogActivationController": CatalogActivationController,
            "CatalogActivationError": CatalogActivationError,
            "CatalogActivationState": CatalogActivationState,
            "CatalogActivationStore": CatalogActivationStore,
        }[name]
    if name == "SqliteCatalogActivationStore":
        from .activation_sqlite import SqliteCatalogActivationStore

        return SqliteCatalogActivationStore
    if name in {
        "DeploymentActivationAuditEvent",
        "DeploymentActivationController",
        "DeploymentActivationError",
        "DeploymentActivationState",
        "DeploymentActivationStore",
        "DeploymentArtifact",
        "DeploymentManifest",
        "DeploymentReadContext",
    }:
        from .deployment import (
            DeploymentActivationAuditEvent,
            DeploymentActivationController,
            DeploymentActivationError,
            DeploymentActivationState,
            DeploymentActivationStore,
            DeploymentArtifact,
            DeploymentManifest,
            DeploymentReadContext,
        )

        return {
            "DeploymentActivationAuditEvent": DeploymentActivationAuditEvent,
            "DeploymentActivationController": DeploymentActivationController,
            "DeploymentActivationError": DeploymentActivationError,
            "DeploymentActivationState": DeploymentActivationState,
            "DeploymentActivationStore": DeploymentActivationStore,
            "DeploymentArtifact": DeploymentArtifact,
            "DeploymentManifest": DeploymentManifest,
            "DeploymentReadContext": DeploymentReadContext,
        }[name]
    if name == "SqliteDeploymentActivationStore":
        from .deployment_sqlite import SqliteDeploymentActivationStore

        return SqliteDeploymentActivationStore
    if name in {
        "LocalProviderHandle",
        "LocalProviderObservationError",
        "deployment_manifest_from_provider_handles",
        "observe_local_provider_handles",
    }:
        from .providers import (
            LocalProviderHandle,
            LocalProviderObservationError,
            deployment_manifest_from_provider_handles,
            observe_local_provider_handles,
        )

        return {
            "LocalProviderHandle": LocalProviderHandle,
            "LocalProviderObservationError": LocalProviderObservationError,
            "deployment_manifest_from_provider_handles": deployment_manifest_from_provider_handles,
            "observe_local_provider_handles": observe_local_provider_handles,
        }[name]
    raise AttributeError(name)
