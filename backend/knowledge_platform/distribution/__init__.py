"""Distribution-boundary contracts for the staged Platform extraction."""

from .boundary import (
    DistributionBoundaryError,
    DistributionBoundaryManifest,
    DistributionPathRule,
    build_phase9_boundary_manifest,
)
from .dependency_closure import (
    DependencyClosureError,
    DependencyClosureFinding,
    DependencyClosureResult,
    build_dependency_closure_shadow,
)
from .dependency_lock import DependencyLockError, DependencyLockPreflight, build_dependency_lock_preflight
from .dependency_sbom import DependencySbomError, DependencySbomShadow, build_dependency_sbom_shadow
from .evidence_bundle import EvidenceBundleError, build_evidence_bundle
from .extraction import (
    ExtractionManifest,
    ExtractionPathPlan,
    ExtractionPreflightError,
    build_phase10_extraction_manifest,
)
from .development_source_snapshot import (
    DevelopmentSourceSnapshotError,
    build_development_source_snapshot,
)
from .harness_dependency_remediation import (
    HarnessDependencyRemediationError,
    HarnessDependencyRemediationPlan,
    RemediationFile,
    RemediationRule,
    build_harness_dependency_remediation_plan,
)
from .harness_dependency_scan import (
    DependencyFinding,
    HarnessDependencyScanError,
    HarnessDependencyScanResult,
    scan_harness_dependency_shadow,
)
from .installation_migration import (
    MIGRATION_DOMAINS,
    SOURCE_WRITER,
    TARGET_WRITERS,
    CredentialRebind,
    InstallationMigrationError,
    InstallationMigrationManifest,
    MigrationObjectSummary,
    MigrationState,
    PartialTargetImportReplay,
    StatefulRollbackReplay,
    replay_partial_target_import_shadow,
    replay_stateful_rollback_shadow,
)
from .local_catalog_inventory import LocalCatalogInventory, LocalCatalogInventoryError, read_local_catalog_inventory
from .package_build import (
    PackageArchiveObservation,
    PackageBuildShadow,
    PackageBuildShadowError,
    PackageCommandResult,
    build_package_shadow,
)
from .rc_validation import (
    RcCheck,
    RcValidationError,
    RcValidationManifest,
    build_rc_validation_manifest,
    stable_digest,
)
from .sbom import InventoryFile, SourceInventory, SourceInventoryError, build_source_inventory

__all__ = [
    "DistributionBoundaryError",
    "DistributionBoundaryManifest",
    "DistributionPathRule",
    "build_phase9_boundary_manifest",
    "DependencyClosureError",
    "DependencyClosureFinding",
    "DependencyClosureResult",
    "build_dependency_closure_shadow",
    "DependencyLockError",
    "DependencyLockPreflight",
    "build_dependency_lock_preflight",
    "DependencySbomError",
    "DependencySbomShadow",
    "build_dependency_sbom_shadow",
    "EvidenceBundleError",
    "build_evidence_bundle",
    "ExtractionManifest",
    "ExtractionPathPlan",
    "ExtractionPreflightError",
    "build_phase10_extraction_manifest",
    "DevelopmentSourceSnapshotError",
    "build_development_source_snapshot",
    "CredentialRebind",
    "InstallationMigrationError",
    "InstallationMigrationManifest",
    "MIGRATION_DOMAINS",
    "MigrationObjectSummary",
    "MigrationState",
    "PartialTargetImportReplay",
    "StatefulRollbackReplay",
    "replay_partial_target_import_shadow",
    "replay_stateful_rollback_shadow",
    "SOURCE_WRITER",
    "TARGET_WRITERS",
    "RcCheck",
    "RcValidationError",
    "RcValidationManifest",
    "build_rc_validation_manifest",
    "stable_digest",
    "DependencyFinding",
    "HarnessDependencyScanError",
    "HarnessDependencyScanResult",
    "scan_harness_dependency_shadow",
    "LocalCatalogInventory",
    "LocalCatalogInventoryError",
    "read_local_catalog_inventory",
    "HarnessDependencyRemediationError",
    "HarnessDependencyRemediationPlan",
    "RemediationFile",
    "RemediationRule",
    "build_harness_dependency_remediation_plan",
    "InventoryFile",
    "SourceInventory",
    "SourceInventoryError",
    "build_source_inventory",
    "PackageBuildShadow",
    "PackageBuildShadowError",
    "PackageArchiveObservation",
    "PackageCommandResult",
    "build_package_shadow",
]
