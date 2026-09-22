"""Prove candidate search readiness through the runtime Catalog provider."""
from __future__ import annotations

import argparse
from collections.abc import Mapping
import json
from pathlib import Path

from knowledge_contracts import Correlation, Principal
from knowledge_platform.catalog import CatalogQueryService, SqliteCatalogQueryRepository
from . import cutover_readiness as readiness


FORMAT = readiness.INDEX_READINESS_FORMAT


class IndexReadinessError(ValueError):
    pass


def _verify_runtime_search(candidate: Path | str, expected_ids: list[str]) -> tuple[str, str]:
    catalog = readiness._path(Path(candidate).expanduser()) / "catalog.sqlite3"
    before = readiness._read_private(catalog, readiness._MAX_CANDIDATE_FILE)
    repository = SqliteCatalogQueryRepository(catalog)
    service = CatalogQueryService(repository)
    principal = Principal("cutover-readiness", scopes=("knowledge.list", "knowledge.search", "knowledge.read"))
    listed = service.list_assets(principal=principal, correlation=Correlation("cutover-readiness-list"))
    listed_ids = sorted(f"asset:{item['id']}" for item in (listed.data or {}).get("assets", ())) if listed.status == "ok" else []
    if listed_ids != expected_ids:
        raise IndexReadinessError("runtime Catalog provider does not enumerate the candidate object set")
    for target_id in expected_ids:
        asset_id = target_id.removeprefix("asset:")
        read = service.read_asset(
            principal=principal,
            correlation=Correlation("cutover-readiness-read-" + asset_id),
            asset_id=asset_id,
        )
        asset = (read.data or {}).get("asset") if read.status == "ok" else None
        if not isinstance(asset, Mapping) or asset.get("id") != asset_id:
            raise IndexReadinessError("runtime Catalog provider cannot read a candidate object")
        probe = next((str(asset.get(field)).strip() for field in ("title", "description") if str(asset.get(field) or "").strip()), "")
        if not probe:
            raise IndexReadinessError("candidate object has no runtime-searchable metadata")
        searched = service.search_assets(
            principal=principal,
            correlation=Correlation("cutover-readiness-search-" + asset_id),
            text=probe,
            limit=100,
        )
        found = {str(item.get("id")) for item in (searched.data or {}).get("assets", ())} if searched.status == "ok" else set()
        if asset_id not in found:
            raise IndexReadinessError("runtime Catalog search cannot retrieve a candidate object")
    after = readiness._read_private(catalog, readiness._MAX_CANDIDATE_FILE)
    if after != before:
        raise IndexReadinessError("candidate Catalog changed during runtime query verification")
    return readiness._digest(before), repository.catalog_revision


def produce_index_readiness(
    *, migration_receipt: Path | str, candidate: Path | str, domain_coverage: Path | str,
    output: Path | str,
) -> dict:
    migration_raw = readiness._read_private(migration_receipt)
    migration = readiness._object(migration_raw, "migration receipt")
    candidate_manifest, candidate_manifest_digest, tree_digest = readiness._verify_candidate(candidate, migration)
    coverage_raw = readiness._read_private(domain_coverage)
    coverage = readiness._object(coverage_raw, "domain coverage")
    source_identity = readiness._require_digest(migration.get("source_snapshot_identity"), "source snapshot identity")
    readiness._verify_domain_coverage(
        coverage,
        installation_id=candidate_manifest["plan"]["installation_id"],
        source_revision=candidate_manifest["plan"]["source_revision"],
        source_snapshot_identity=source_identity,
        migration_receipt_digest=readiness._digest(migration_raw),
        candidate_manifest_digest=candidate_manifest_digest,
        candidate_tree_digest=tree_digest,
        candidate_asset_ids=set(candidate_manifest["asset_bindings"]),
    )
    catalog_domain = next(item for item in coverage["domains"] if item["domain"] == "knowledge_catalog")
    indexable_ids = catalog_domain["target"]["inventory"]
    if not indexable_ids:
        state, indexes = "explicit_absent", []
        absence = {"no_indexable_objects": True, "source_enumerated": True}
    else:
        artifact_digest, catalog_revision = _verify_runtime_search(candidate, indexable_ids)
        if catalog_revision != artifact_digest:
            # SqliteCatalogQueryRepository's revision includes the filename and
            # optional sidecars, so bind it into the provider input commitment.
            provider_input = readiness._digest(readiness._encode({
                "candidate_objects": indexable_ids,
                "catalog_revision": catalog_revision,
            }))
        else:
            provider_input = readiness._digest(readiness._encode(indexable_ids))
        indexes = [{
            "id": "catalog-metadata-search",
            "status": "ready",
            "input_sha256": provider_input,
            "artifact_sha256": artifact_digest,
        }]
        state, absence = "ready", None
    result = {
        "format": FORMAT,
        "producer": "puddingknowledge",
        "installation_id": candidate_manifest["plan"]["installation_id"],
        "source_revision": candidate_manifest["plan"]["source_revision"],
        "domain_coverage_sha256": readiness._digest(coverage_raw),
        "candidate_tree_sha256": tree_digest,
        "state": state,
        "indexes": indexes,
        "absence_attestation": absence,
    }
    readiness._verify_indexes(
        result,
        installation_id=result["installation_id"],
        source_revision=result["source_revision"],
        domain_coverage_digest=result["domain_coverage_sha256"],
        candidate_tree_digest=tree_digest,
    )
    readiness._publish(Path(output), readiness._encode(result))
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--migration-receipt", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--domain-coverage", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = produce_index_readiness(**vars(args))
    except Exception:
        print(json.dumps({"format": FORMAT, "status": "error", "error_code": "index_readiness_rejected"}, sort_keys=True))
        return 1
    print(json.dumps({"format": FORMAT, "state": result["state"], "ready_count": len(result["indexes"])}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
