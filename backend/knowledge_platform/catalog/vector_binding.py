"""Fail-closed audit for Catalog to Vector-index identity bindings."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from .query import CatalogQueryRepository


@dataclass(frozen=True, slots=True)
class VectorCatalogBindingAudit:
    """Portable binding evidence; no provider paths, URLs, or hit contents."""

    collection_found: bool
    capability_declared: bool
    explicit_provider_binding: bool
    catalog_asset_count: int
    vector_identity_count: int
    matched_identity_count: int
    activation_allowed: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "collection_found": self.collection_found,
            "capability_declared": self.capability_declared,
            "explicit_provider_binding": self.explicit_provider_binding,
            "catalog_asset_count": self.catalog_asset_count,
            "vector_identity_count": self.vector_identity_count,
            "matched_identity_count": self.matched_identity_count,
            "identity_coverage": (
                self.matched_identity_count / self.vector_identity_count
                if self.vector_identity_count
                else 0.0
            ),
            "activation_allowed": self.activation_allowed,
        }


def audit_vector_catalog_binding(
    *,
    catalog: CatalogQueryRepository,
    space_id: str,
    collection_id: str,
    collection_version: str,
    capability: str,
    vector_document_ids: Iterable[str],
) -> VectorCatalogBindingAudit:
    """Require an explicit Catalog binding and exact per-document identity coverage.

    A provider's collection name or row count is not a provenance binding.  The
    index may only become activatable when every audited Vector ``doc_id`` is an
    exact Catalog Asset identity in the selected, capability-approved Collection.
    """

    vector_ids = {value for value in vector_document_ids if isinstance(value, str) and value}
    collection = next(
        (
            item
            for item in catalog.list_collections(space_id=space_id)
            if str(item.get("id") or "") == collection_id
            and str(item.get("version") or "") == collection_version
        ),
        None,
    )
    if collection is None:
        return VectorCatalogBindingAudit(False, False, False, 0, len(vector_ids), 0, False)
    capabilities = collection.get("capabilities", [])
    bindings = collection.get("provider_bindings", {})
    asset_ids = {
        str(value)
        for value in collection.get("asset_ids", [])
        if isinstance(value, str) and value
    }
    explicit_binding = isinstance(bindings, dict) and isinstance(bindings.get(capability), dict)
    matched = len(vector_ids & asset_ids)
    capability_declared = isinstance(capabilities, list) and capability in capabilities
    activation_allowed = bool(
        capability_declared
        and explicit_binding
        and vector_ids
        and matched == len(vector_ids)
    )
    return VectorCatalogBindingAudit(
        collection_found=True,
        capability_declared=capability_declared,
        explicit_provider_binding=explicit_binding,
        catalog_asset_count=len(asset_ids),
        vector_identity_count=len(vector_ids),
        matched_identity_count=matched,
        activation_allowed=activation_allowed,
    )


__all__ = ["VectorCatalogBindingAudit", "audit_vector_catalog_binding"]
