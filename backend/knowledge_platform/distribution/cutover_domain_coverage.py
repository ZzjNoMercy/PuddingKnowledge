"""Assemble domain coverage exclusively from trusted producer inventories."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
from urllib.parse import quote

from . import cutover_readiness as readiness


FORMAT = readiness.DOMAIN_COVERAGE_FORMAT


class DomainCoverageError(ValueError):
    pass


def _knowledge_mappings(candidate: Path | str, source_ids: list[str], target_ids: list[str]) -> list[dict[str, str]]:
    root = readiness._path(Path(candidate).expanduser())
    catalog = root / "catalog.sqlite3"
    before = readiness._read_private(catalog, readiness._MAX_CANDIDATE_FILE)
    connection = sqlite3.connect(f"file:{quote(str(catalog), safe='/')}?mode=ro&immutable=1", uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise DomainCoverageError("candidate Catalog failed integrity validation")
        rows = connection.execute("SELECT id, metadata_json FROM knowledge_assets ORDER BY id").fetchall()
    finally:
        connection.close()
    if readiness._read_private(catalog, readiness._MAX_CANDIDATE_FILE) != before:
        raise DomainCoverageError("candidate Catalog changed during mapping derivation")
    mappings = []
    for asset_id, raw_metadata in rows:
        try:
            metadata = json.loads(raw_metadata)
        except (TypeError, json.JSONDecodeError) as error:
            raise DomainCoverageError("candidate asset metadata is invalid") from error
        legacy_id = metadata.get("legacy_document_id") if isinstance(metadata, dict) else None
        source_id, target_id = f"document:{legacy_id}", f"asset:{asset_id}"
        if not isinstance(legacy_id, str) or not readiness._TOKEN.fullmatch(source_id) or not readiness._TOKEN.fullmatch(target_id):
            raise DomainCoverageError("candidate asset lacks a portable legacy identity")
        mappings.append({"source_id": source_id, "target_id": target_id, "resource_uri": f"knowledge://assets/{asset_id}"})
    mappings.sort(key=lambda item: (item["source_id"], item["target_id"]))
    if sorted(item["source_id"] for item in mappings) != source_ids or sorted(item["target_id"] for item in mappings) != target_ids:
        raise DomainCoverageError("candidate document-to-asset mapping does not cover producer inventories")
    return mappings


def _producer(path: Path | str, *, source: bool, domain: str,
              source_snapshot_identity: str, candidate_tree_digest: str) -> dict:
    raw = readiness._read_private(path)
    value = readiness._object(raw, f"{domain} {'source' if source else 'target'} inventory")
    expected = (
        {"format", "producer", "source_snapshot_identity", "inventory", "inventory_sha256"}
        if source else
        {"format", "producer", "target_artifact_sha256", "inventory", "inventory_sha256"}
    )
    if set(value) != expected:
        raise DomainCoverageError("domain producer receipt contract is invalid")
    if source:
        if (
            value.get("format") != "puddingclaw-cutover-domain-inventory/v1"
            or value.get("producer") != "puddingclaw"
            or value.get("source_snapshot_identity") != source_snapshot_identity
        ):
            raise DomainCoverageError("source inventory is not bound to this snapshot")
    else:
        expected_format = "puddingharness-cutover-domain-inventory/v1" if domain == "session_harness" else "puddingknowledge-cutover-domain-inventory/v1"
        expected_producer = "puddingharness" if domain == "session_harness" else "puddingknowledge"
        if value.get("format") != expected_format or value.get("producer") != expected_producer:
            raise DomainCoverageError("target inventory producer is not trusted")
        readiness._require_digest(value.get("target_artifact_sha256"), "target artifact")
        if domain != "session_harness" and value["target_artifact_sha256"] != candidate_tree_digest:
            raise DomainCoverageError("Knowledge inventory is not bound to this candidate")
    inventory = value.get("inventory")
    if (
        not isinstance(inventory, list)
        or inventory != sorted(inventory)
        or len(inventory) != len(set(inventory))
        or any(not isinstance(item, str) or not readiness._TOKEN.fullmatch(item) for item in inventory)
        or value.get("inventory_sha256") != readiness._digest(readiness._encode(inventory))
    ):
        raise DomainCoverageError("producer inventory is not a canonical enumerated object set")
    return value


def assemble_domain_coverage(
    *, migration_receipt: Path | str, candidate: Path | str,
    source_session_inventory: Path | str, target_session_inventory: Path | str,
    source_knowledge_inventory: Path | str, target_knowledge_inventory: Path | str,
    source_connector_inventory: Path | str, target_connector_inventory: Path | str,
    output: Path | str,
) -> dict:
    migration_raw = readiness._read_private(migration_receipt)
    migration = readiness._object(migration_raw, "migration receipt")
    manifest, manifest_digest, tree_digest = readiness._verify_candidate(candidate, migration)
    source_snapshot_identity = readiness._require_digest(migration.get("source_snapshot_identity"), "source snapshot identity")
    paths = {
        "session_harness": (source_session_inventory, target_session_inventory),
        "knowledge_catalog": (source_knowledge_inventory, target_knowledge_inventory),
        "connector_jobs": (source_connector_inventory, target_connector_inventory),
    }
    domains = []
    for domain in readiness.MIGRATION_DOMAINS:
        source_path, target_path = paths[domain]
        source = _producer(source_path, source=True, domain=domain,
                           source_snapshot_identity=source_snapshot_identity, candidate_tree_digest=tree_digest)
        target = _producer(target_path, source=False, domain=domain,
                           source_snapshot_identity=source_snapshot_identity, candidate_tree_digest=tree_digest)
        if domain == "knowledge_catalog":
            mappings = _knowledge_mappings(candidate, source["inventory"], target["inventory"])
        else:
            if source["inventory"] != target["inventory"]:
                raise DomainCoverageError(f"{domain} source and target object sets differ")
            prefix = "harness" if domain == "session_harness" else "knowledge"
            mappings = [
                {"source_id": item, "target_id": item, "resource_uri": f"{prefix}://migration/{domain}/{item}"}
                for item in source["inventory"]
            ]
        domains.append({
            "domain": domain,
            "source": source,
            "target": target,
            "mapping": mappings,
            "zero_object_attestation": None if mappings else {
                "mapping_not_required": True,
                "source_enumerated": True,
                "target_enumerated": True,
            },
        })
    result = {
        "format": FORMAT,
        "installation_id": manifest["plan"]["installation_id"],
        "source_revision": manifest["plan"]["source_revision"],
        "migration_receipt_sha256": readiness._digest(migration_raw),
        "candidate_manifest_sha256": manifest_digest,
        "domains": domains,
    }
    candidate_assets = set(manifest["asset_bindings"])
    readiness._verify_domain_coverage(
        result,
        installation_id=result["installation_id"],
        source_revision=result["source_revision"],
        source_snapshot_identity=source_snapshot_identity,
        migration_receipt_digest=result["migration_receipt_sha256"],
        candidate_manifest_digest=manifest_digest,
        candidate_tree_digest=tree_digest,
        candidate_asset_ids=candidate_assets,
    )
    readiness._publish(Path(output), readiness._encode(result))
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--migration-receipt", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--source-session-inventory", type=Path, required=True)
    parser.add_argument("--target-session-inventory", type=Path, required=True)
    parser.add_argument("--source-knowledge-inventory", type=Path, required=True)
    parser.add_argument("--target-knowledge-inventory", type=Path, required=True)
    parser.add_argument("--source-connector-inventory", type=Path, required=True)
    parser.add_argument("--target-connector-inventory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = assemble_domain_coverage(**vars(args))
    except Exception:
        print(json.dumps({"format": FORMAT, "status": "error", "error_code": "domain_coverage_rejected"}, sort_keys=True))
        return 1
    print(json.dumps({"format": FORMAT, "domain_count": len(result["domains"])}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
