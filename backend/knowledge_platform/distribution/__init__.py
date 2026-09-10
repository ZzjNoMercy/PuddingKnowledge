"""Distribution contracts, loaded on demand without development-only dependencies."""
from importlib import import_module

_EXPORTS = {
    'DistributionBoundaryError': ('boundary', 'DistributionBoundaryError'),
    'DistributionBoundaryManifest': ('boundary', 'DistributionBoundaryManifest'),
    'DistributionPathRule': ('boundary', 'DistributionPathRule'),
    'build_phase9_boundary_manifest': ('boundary', 'build_phase9_boundary_manifest'),
    'DependencyClosureError': ('dependency_closure', 'DependencyClosureError'),
    'DependencyClosureFinding': ('dependency_closure', 'DependencyClosureFinding'),
    'DependencyClosureResult': ('dependency_closure', 'DependencyClosureResult'),
    'build_dependency_closure_shadow': ('dependency_closure', 'build_dependency_closure_shadow'),
    'DependencyLockError': ('dependency_lock', 'DependencyLockError'),
    'DependencyLockPreflight': ('dependency_lock', 'DependencyLockPreflight'),
    'build_dependency_lock_preflight': ('dependency_lock', 'build_dependency_lock_preflight'),
    'DependencySbomError': ('dependency_sbom', 'DependencySbomError'),
    'DependencySbomShadow': ('dependency_sbom', 'DependencySbomShadow'),
    'build_dependency_sbom_shadow': ('dependency_sbom', 'build_dependency_sbom_shadow'),
    'EvidenceBundleError': ('evidence_bundle', 'EvidenceBundleError'),
    'build_evidence_bundle': ('evidence_bundle', 'build_evidence_bundle'),
    'ExtractionManifest': ('extraction', 'ExtractionManifest'),
    'ExtractionPathPlan': ('extraction', 'ExtractionPathPlan'),
    'ExtractionPreflightError': ('extraction', 'ExtractionPreflightError'),
    'build_phase10_extraction_manifest': ('extraction', 'build_phase10_extraction_manifest'),
    'DevelopmentSourceSnapshotError': ('development_source_snapshot', 'DevelopmentSourceSnapshotError'),
    'build_development_source_snapshot': ('development_source_snapshot', 'build_development_source_snapshot'),
    'HarnessDependencyRemediationError': ('harness_dependency_remediation', 'HarnessDependencyRemediationError'),
    'HarnessDependencyRemediationPlan': ('harness_dependency_remediation', 'HarnessDependencyRemediationPlan'),
    'RemediationFile': ('harness_dependency_remediation', 'RemediationFile'),
    'RemediationRule': ('harness_dependency_remediation', 'RemediationRule'),
    'build_harness_dependency_remediation_plan': ('harness_dependency_remediation', 'build_harness_dependency_remediation_plan'),
    'DependencyFinding': ('harness_dependency_scan', 'DependencyFinding'),
    'HarnessDependencyScanError': ('harness_dependency_scan', 'HarnessDependencyScanError'),
    'HarnessDependencyScanResult': ('harness_dependency_scan', 'HarnessDependencyScanResult'),
    'scan_harness_dependency_shadow': ('harness_dependency_scan', 'scan_harness_dependency_shadow'),
    'MIGRATION_DOMAINS': ('installation_migration', 'MIGRATION_DOMAINS'),
    'SOURCE_WRITER': ('installation_migration', 'SOURCE_WRITER'),
    'TARGET_WRITERS': ('installation_migration', 'TARGET_WRITERS'),
    'CredentialRebind': ('installation_migration', 'CredentialRebind'),
    'InstallationMigrationError': ('installation_migration', 'InstallationMigrationError'),
    'InstallationMigrationManifest': ('installation_migration', 'InstallationMigrationManifest'),
    'MigrationObjectSummary': ('installation_migration', 'MigrationObjectSummary'),
    'MigrationState': ('installation_migration', 'MigrationState'),
    'PartialTargetImportReplay': ('installation_migration', 'PartialTargetImportReplay'),
    'StatefulRollbackReplay': ('installation_migration', 'StatefulRollbackReplay'),
    'replay_partial_target_import_shadow': ('installation_migration', 'replay_partial_target_import_shadow'),
    'replay_stateful_rollback_shadow': ('installation_migration', 'replay_stateful_rollback_shadow'),
    'LocalCatalogInventory': ('local_catalog_inventory', 'LocalCatalogInventory'),
    'LocalCatalogInventoryError': ('local_catalog_inventory', 'LocalCatalogInventoryError'),
    'read_local_catalog_inventory': ('local_catalog_inventory', 'read_local_catalog_inventory'),
    'PackageArchiveObservation': ('package_build', 'PackageArchiveObservation'),
    'PackageBuildShadow': ('package_build', 'PackageBuildShadow'),
    'PackageBuildShadowError': ('package_build', 'PackageBuildShadowError'),
    'PackageCommandResult': ('package_build', 'PackageCommandResult'),
    'build_package_shadow': ('package_build', 'build_package_shadow'),
    'RcCheck': ('rc_validation', 'RcCheck'),
    'RcValidationError': ('rc_validation', 'RcValidationError'),
    'RcValidationManifest': ('rc_validation', 'RcValidationManifest'),
    'build_rc_validation_manifest': ('rc_validation', 'build_rc_validation_manifest'),
    'stable_digest': ('rc_validation', 'stable_digest'),
    'InventoryFile': ('sbom', 'InventoryFile'),
    'SourceInventory': ('sbom', 'SourceInventory'),
    'SourceInventoryError': ('sbom', 'SourceInventoryError'),
    'build_source_inventory': ('sbom', 'build_source_inventory'),
}
__all__ = list(_EXPORTS)


def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module, attribute = _EXPORTS[name]
    value = getattr(import_module(f".{module}", __name__), attribute)
    globals()[name] = value
    return value
