"""Application service for exporting a Package from a Catalog repository."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .builder import KnowledgePackageBuilder, PackageBuildResult


@dataclass(frozen=True, slots=True)
class CatalogPackageSnapshot:
    """One internally consistent, content-addressed Catalog read."""

    catalog_revision: str
    spaces: list[Mapping[str, Any]]
    collections: list[Mapping[str, Any]]
    assets: list[Mapping[str, Any]]
    semantic_assets: list[Mapping[str, Any]]
    database_sources: list[Mapping[str, Any]] = field(default_factory=list)
    provider_versions: Mapping[str, Any] = field(default_factory=dict)


class CatalogPackageSource(Protocol):
    """Read-only source that must provide an atomic Package snapshot."""

    def read_package_snapshot(self) -> CatalogPackageSnapshot: ...


class PackageExportService:
    """Build only from a repository snapshot, never from caller-supplied metadata."""

    def __init__(self, source: CatalogPackageSource, builder: KnowledgePackageBuilder | None = None) -> None:
        self._source = source
        self._builder = builder or KnowledgePackageBuilder()

    def export_snapshot(
        self,
        *,
        output_dir: Path,
        package_id: str,
        version: str,
        asset_files: Mapping[str, Path],
        capabilities: list[str],
    ) -> PackageBuildResult:
        snapshot = self._source.read_package_snapshot()
        return self._builder.build(
            output_dir=output_dir,
            package_id=package_id,
            version=version,
            spaces=snapshot.spaces,
            collections=snapshot.collections,
            assets=snapshot.assets,
            asset_files=asset_files,
            capabilities=capabilities,
            catalog_revision=snapshot.catalog_revision,
            semantic_assets=snapshot.semantic_assets,
            database_sources=snapshot.database_sources,
            provider_versions=snapshot.provider_versions,
        )
