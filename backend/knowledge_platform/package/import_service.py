"""Application service for validated Package import and Vanna rebuild."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .builder import PackageValidationResult, import_package_zip

if TYPE_CHECKING:
    from knowledge_platform.database.vanna_rebuild import VannaIndexRebuilder, VannaRebuildResult


@dataclass(frozen=True, slots=True)
class PackageImportResult:
    """Result returned only after Package validation and index commit succeed."""

    package: PackageValidationResult
    vanna: VannaRebuildResult


class PackageImportService:
    """Import a Package as a validated staging unit, then rebuild Vanna evidence."""

    def __init__(self, *, rebuilder: VannaIndexRebuilder) -> None:
        self._rebuilder = rebuilder

    def import_and_rebuild(self, *, package_zip: Path, output_dir: Path) -> PackageImportResult:
        """Validate the ZIP and rebuild indexes from portable Package evidence.

        ``import_package_zip`` publishes only a fully validated Package tree.
        A caller must treat the returned result as the activation boundary:
        if rebuilding raises, no successful import result is returned and the
        rebuilder's abort contract is responsible for preventing active index
        visibility.
        """

        # Keep the package/database module boundary lazy: database.vanna_rebuild
        # itself validates Package files and imports the package builder.
        from knowledge_platform.database.vanna_rebuild import rebuild_vanna_indexes

        package = import_package_zip(package_zip, output_dir)
        vanna = rebuild_vanna_indexes(package_root=package.package_root, rebuilder=self._rebuilder)
        return PackageImportResult(package=package, vanna=vanna)
