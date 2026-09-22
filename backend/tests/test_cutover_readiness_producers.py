import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from knowledge_platform.distribution import cutover_readiness as readiness
from knowledge_platform.distribution.cutover_domain_coverage import (
    DomainCoverageError,
    assemble_domain_coverage,
)
from knowledge_platform.distribution.cutover_domain_inventory import (
    DomainInventoryError,
    produce_inventory,
)
from knowledge_platform.distribution.cutover_index_readiness import (
    IndexReadinessError,
    produce_index_readiness,
)
from test_cutover_readiness import _inputs


def _encode(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _digest(value) -> str:
    raw = value if isinstance(value, bytes) else _encode(value)
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _private(path: Path, value) -> Path:
    path.write_bytes(value if isinstance(value, bytes) else _encode(value))
    path.chmod(0o600)
    return path


def _producer_inputs(tmp_path: Path):
    migration_root = tmp_path / "migration"
    migration_root.mkdir(mode=0o700)
    paths = _inputs(migration_root)
    migration = json.loads(paths["migration_receipt"].read_text())
    knowledge = produce_inventory(
        migration_receipt=paths["migration_receipt"], candidate=paths["candidate"],
        domain="knowledge_catalog", output=tmp_path / "target-knowledge.json",
    )
    connectors = produce_inventory(
        migration_receipt=paths["migration_receipt"], candidate=paths["candidate"],
        domain="connector_jobs", output=tmp_path / "target-connectors.json",
    )

    def source(name, inventory):
        return _private(tmp_path / name, {
            "format": "puddingclaw-cutover-domain-inventory/v1",
            "producer": "puddingclaw",
            "source_snapshot_identity": migration["source_snapshot_identity"],
            "inventory": inventory,
            "inventory_sha256": _digest(inventory),
        })

    source_session = source("source-session.json", [])
    target_session = _private(tmp_path / "target-session.json", {
        "format": "puddingharness-cutover-domain-inventory/v1",
        "producer": "puddingharness",
        "target_artifact_sha256": "sha256:" + "e" * 64,
        "inventory": [],
        "inventory_sha256": _digest([]),
    })
    arguments = {
        "migration_receipt": paths["migration_receipt"],
        "candidate": paths["candidate"],
        "source_session_inventory": source_session,
        "target_session_inventory": target_session,
        "source_knowledge_inventory": source(
            "source-knowledge.json",
            ["document:" + item.split("asset_", 1)[1].rsplit("_", 1)[0] for item in knowledge["inventory"]],
        ),
        "target_knowledge_inventory": tmp_path / "target-knowledge.json",
        "source_connector_inventory": source("source-connectors.json", connectors["inventory"]),
        "target_connector_inventory": tmp_path / "target-connectors.json",
        "output": tmp_path / "coverage.json",
    }
    return paths, arguments


def test_target_inventory_is_bound_to_verified_candidate_bytes(tmp_path: Path) -> None:
    migration_root = tmp_path / "migration"
    migration_root.mkdir(mode=0o700)
    paths = _inputs(migration_root)
    catalog = produce_inventory(
        migration_receipt=paths["migration_receipt"], candidate=paths["candidate"],
        domain="knowledge_catalog", output=tmp_path / "catalog.json",
    )
    connectors = produce_inventory(
        migration_receipt=paths["migration_receipt"], candidate=paths["candidate"],
        domain="connector_jobs", output=tmp_path / "connectors.json",
    )
    assert catalog["inventory"] and all(item.startswith("asset:") for item in catalog["inventory"])
    assert connectors["inventory"] == []
    assert catalog["target_artifact_sha256"] == connectors["target_artifact_sha256"]
    assert json.loads((tmp_path / "catalog.json").read_text()) == catalog

    (paths["candidate"] / "catalog.sqlite3").write_bytes(b"changed")
    with pytest.raises((DomainInventoryError, readiness.CutoverReadinessError)):
        produce_inventory(
            migration_receipt=paths["migration_receipt"], candidate=paths["candidate"],
            domain="connector_jobs", output=tmp_path / "changed.json",
        )


def test_coverage_is_assembled_from_matching_actual_inventories(tmp_path: Path) -> None:
    _paths, arguments = _producer_inputs(tmp_path)
    result = assemble_domain_coverage(**arguments)
    assert [item["domain"] for item in result["domains"]] == list(readiness.MIGRATION_DOMAINS)
    assert result["domains"][1]["mapping"]
    assert result["domains"][1]["mapping"] == [{
        "source_id": "document:doc-1",
        "target_id": next(item for item in result["domains"][1]["target"]["inventory"]),
        "resource_uri": "knowledge://assets/" + next(item for item in result["domains"][1]["target"]["inventory"]).removeprefix("asset:"),
    }]
    assert result["domains"][0]["zero_object_attestation"]["source_enumerated"] is True
    assert json.loads(arguments["output"].read_text()) == result


@pytest.mark.parametrize("mutation", ["identity", "digest", "target_gap", "forged_derivation"])
def test_coverage_rejects_cross_snapshot_or_self_attested_sets(tmp_path: Path, mutation: str) -> None:
    _paths, arguments = _producer_inputs(tmp_path)
    source_path = arguments["source_knowledge_inventory"]
    value = json.loads(source_path.read_text())
    if mutation == "identity":
        value["source_snapshot_identity"] = "sha256:" + "1" * 64
    elif mutation == "digest":
        value["inventory_sha256"] = "sha256:" + "2" * 64
    elif mutation == "target_gap":
        value["inventory"] = []
        value["inventory_sha256"] = _digest([])
    else:
        value["inventory"] = ["document:forged"]
        value["inventory_sha256"] = _digest(value["inventory"])
    _private(source_path, value)
    with pytest.raises(DomainCoverageError):
        assemble_domain_coverage(**arguments)


def test_index_readiness_requires_real_candidate_bound_object_index(tmp_path: Path) -> None:
    paths, arguments = _producer_inputs(tmp_path)
    coverage = assemble_domain_coverage(**arguments)
    object_ids = next(item for item in coverage["domains"] if item["domain"] == "knowledge_catalog")["target"]["inventory"]
    result = produce_index_readiness(
        migration_receipt=paths["migration_receipt"], candidate=paths["candidate"],
        domain_coverage=arguments["output"],
        output=tmp_path / "index-readiness.json",
    )
    assert result["state"] == "ready"
    assert result["indexes"][0]["id"] == "catalog-metadata-search"
    assert result["indexes"][0]["artifact_sha256"] == _digest((paths["candidate"] / "catalog.sqlite3").read_bytes())
    assert json.loads((tmp_path / "index-readiness.json").read_text()) == result
    final = readiness.build_cutover_readiness(
        migration_receipt=paths["migration_receipt"],
        candidate=paths["candidate"],
        credential_rebind_receipt=paths["credential_rebind_receipt"],
        domain_coverage=arguments["output"],
        index_readiness=tmp_path / "index-readiness.json",
        output=tmp_path / "cutover-readiness.json",
    )
    assert final["state"] == "verified_inactive_complete"
    assert final["id_resource_mappings"] == [{
        "domain": "knowledge_catalog",
        "source_id": "document:doc-1",
        "resource_uri": "knowledge://assets/" + object_ids[0].removeprefix("asset:"),
    }]


def test_index_readiness_fails_when_runtime_search_cannot_retrieve_candidate(tmp_path: Path) -> None:
    paths, arguments = _producer_inputs(tmp_path)
    assemble_domain_coverage(**arguments)
    catalog = paths["candidate"] / "catalog.sqlite3"
    connection = sqlite3.connect(catalog)
    try:
        connection.execute("UPDATE knowledge_assets SET title='', description='' ")
        connection.commit()
    finally:
        connection.close()
    # Recommit the candidate artifact so the failure reaches the real runtime
    # query probe rather than stopping at the outer byte-integrity boundary.
    manifest_path = paths["candidate"] / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    digest = _digest(catalog.read_bytes())
    manifest["files"]["catalog.sqlite3"] = digest
    manifest_path.write_bytes(_encode(manifest))
    migration = json.loads(paths["migration_receipt"].read_text())
    migration["artifacts"]["candidate/catalog.sqlite3"] = digest
    migration["artifacts"]["candidate/manifest.json"] = _digest(manifest_path.read_bytes())
    paths["migration_receipt"].write_bytes(_encode(migration))
    produce_inventory(
        migration_receipt=paths["migration_receipt"], candidate=paths["candidate"],
        domain="knowledge_catalog", output=tmp_path / "target-knowledge-mutated.json",
    )
    produce_inventory(
        migration_receipt=paths["migration_receipt"], candidate=paths["candidate"],
        domain="connector_jobs", output=tmp_path / "target-connectors-mutated.json",
    )
    arguments["target_knowledge_inventory"] = tmp_path / "target-knowledge-mutated.json"
    arguments["target_connector_inventory"] = tmp_path / "target-connectors-mutated.json"
    arguments["output"] = tmp_path / "coverage-mutated.json"
    assemble_domain_coverage(**arguments)
    with pytest.raises(IndexReadinessError, match="runtime-searchable"):
        produce_index_readiness(
            migration_receipt=paths["migration_receipt"], candidate=paths["candidate"],
            domain_coverage=arguments["output"],
            output=tmp_path / "forged-readiness.json",
        )
