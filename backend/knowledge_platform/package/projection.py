"""Explicit, narrow projections for legacy Catalog metadata.

Package metadata is portable by contract.  A legacy Catalog may still contain
the old virtual ``/knowledge/`` mount in a human-facing description.  This
module only rewrites that one known marker when the caller explicitly opts in;
unknown paths and secret-bearing values remain the Package Builder's concern
and are rejected there.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from .service import CatalogPackageSnapshot, CatalogPackageSource

_LEGACY_VIRTUAL_MOUNT = re.compile(r"(?<![A-Za-z0-9_.])/knowledge/(?=[\s.,;:!?)}\]]|$)")
_PORTABLE_MOUNT_LABEL = "legacy Knowledge mount"


class LegacyVirtualMountPackageSource:
    """Wrap a Package source with an explicit legacy-mount metadata projection."""

    def __init__(self, source: CatalogPackageSource) -> None:
        self._source = source
        self._report: dict[str, Any] = {
            "requested": True,
            "applied": False,
            "rule": "legacy_virtual_knowledge_mount_to_portable_label",
            "normalized_field_count": 0,
            "normalized_record_count": 0,
            "execution_allowed": False,
            "activation_allowed": False,
        }

    @property
    def report(self) -> dict[str, Any]:
        return dict(self._report)

    def read_package_snapshot(self) -> CatalogPackageSnapshot:
        snapshot = self._source.read_package_snapshot()
        spaces: list[Mapping[str, Any]] = []
        normalized_field_count = 0
        normalized_record_count = 0
        for space in snapshot.spaces:
            projected = dict(space)
            description = projected.get("description")
            if isinstance(description, str):
                normalized = _LEGACY_VIRTUAL_MOUNT.sub(_PORTABLE_MOUNT_LABEL, description)
                if normalized != description:
                    projected["description"] = normalized
                    normalized_field_count += 1
                    normalized_record_count += 1
            spaces.append(projected)
        self._report.update(
            {
                "applied": normalized_field_count > 0,
                "normalized_field_count": normalized_field_count,
                "normalized_record_count": normalized_record_count,
                "catalog_revision": snapshot.catalog_revision,
            }
        )
        return CatalogPackageSnapshot(
            catalog_revision=snapshot.catalog_revision,
            spaces=spaces,
            collections=snapshot.collections,
            assets=snapshot.assets,
            semantic_assets=snapshot.semantic_assets,
            database_sources=snapshot.database_sources,
            provider_versions=snapshot.provider_versions,
        )
