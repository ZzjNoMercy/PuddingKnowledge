from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from knowledge_platform.distribution import cutover_readiness as readiness
from knowledge_platform.distribution import credential_rebind
from knowledge_platform.distribution.migrate_from_claw import migrate_from_claw
from test_migrate_from_claw_protocol import fixture


def _encode(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _digest(value) -> str:
    data = value if isinstance(value, bytes) else _encode(value)
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _private_json(path: Path, value) -> Path:
    path.write_bytes(_encode(value) + b"\n")
    path.chmod(0o600)
    return path


def _domain(domain: str, object_ids: list[str], *, source_snapshot_identity: str, target_artifact_sha256: str):
    ids = sorted(object_ids)
    ids_digest = _digest(ids)
    if ids:
        prefix = "harness" if domain == "session_harness" else "knowledge"
        mappings = [
            {"source_id": item, "target_id": item, "resource_uri": f"{prefix}://migration/{domain}/{item}"}
            for item in ids
        ]
        zero = None
    else:
        mappings = []
        zero = {"mapping_not_required": True, "source_enumerated": True, "target_enumerated": True}
    target_format = (
        "puddingharness-cutover-domain-inventory/v1"
        if domain == "session_harness"
        else "puddingknowledge-cutover-domain-inventory/v1"
    )
    return {
        "domain": domain,
        "source": {
            "format": "puddingclaw-cutover-domain-inventory/v1",
            "producer": "puddingclaw",
            "source_snapshot_identity": source_snapshot_identity,
            "inventory": ids,
            "inventory_sha256": ids_digest,
        },
        "target": {
            "format": target_format,
            "producer": "puddingharness" if domain == "session_harness" else "puddingknowledge",
            "target_artifact_sha256": target_artifact_sha256,
            "inventory": ids,
            "inventory_sha256": ids_digest,
        },
        "mapping": mappings,
        "zero_object_attestation": zero,
    }


def _credential_receipt() -> dict:
    source_digest = "sha256:" + "a" * 64
    entries = []
    rebound = absent = not_applicable = 0
    for name, _fields, kind, _selector, covered_by in credential_rebind._SLOTS:
        base = {
            "slot": name,
            "target_ref": f"credential://users/local/credentials/{name.replace('*', 'reference')}",
            "source_ref_digest": source_digest,
            "materialized": False,
        }
        if name == "database.password":
            entries.append({
                **base,
                "status": "rebound",
                "materialized": True,
                "source_refs": ["database-config"],
                "vault_ref": "vault://users/local/credentials/database.password",
            })
            rebound += 1
        elif kind == "reference":
            entries.append({**base, "status": "not-applicable", "covered_by": list(covered_by)})
            not_applicable += 1
        else:
            entries.append({**base, "status": "absent", "source_refs": []})
            absent += 1
    return {
        "format": "puddingknowledge-credential-rebind/v1",
        "state": "completed",
        "credential_continuity_verified": True,
        "owner_user_id": "local",
        "source": {
            "home": "/legacy/home",
            "authority_provider": "file",
            "authority_key_id": "sha256:" + "b" * 32,
            "fingerprint": {"users/local/credentials/provider-registry.enc": source_digest},
            "fingerprint_unchanged": True,
        },
        "target": {"root": "/target/vault"},
        "slots_selected": list(credential_rebind._SLOT_NAMES),
        "rebinds": entries,
        "skipped": [],
        "not_applicable_artifacts": [],
        "unrecognized_source_artifacts": [],
        "retained_source_refs": [],
        "counts": {"rebound": rebound, "absent": absent, "failed": 0, "not_applicable": not_applicable, "skipped": 0, "retained": 0},
    }


def _inputs(tmp_path: Path, *, indexes: str = "ready") -> dict[str, Path]:
    request, migration_root = fixture(tmp_path)
    migrate_from_claw(request, migration_root, source_snapshot=tmp_path / "snapshot")
    migration_receipt = migration_root / "receipt.json"
    candidate = migration_root / "candidate"
    manifest = json.loads((candidate / "manifest.json").read_text())
    migration_raw = migration_receipt.read_bytes()
    manifest_raw = (candidate / "manifest.json").read_bytes()
    migration = json.loads(migration_raw)
    candidate_tree_digest = _digest({"manifest": _digest(manifest_raw), "files": manifest["files"]})
    coverage_value = {
        "format": readiness.DOMAIN_COVERAGE_FORMAT,
        "installation_id": manifest["plan"]["installation_id"],
        "source_revision": manifest["plan"]["source_revision"],
        "migration_receipt_sha256": _digest(migration_raw),
        "candidate_manifest_sha256": _digest(manifest_raw),
        "domains": [
            _domain("session_harness", [], source_snapshot_identity=migration["source_snapshot_identity"], target_artifact_sha256="sha256:" + "e" * 64),
            _domain("knowledge_catalog", [f"asset:{item}" for item in sorted(manifest["asset_bindings"])], source_snapshot_identity=migration["source_snapshot_identity"], target_artifact_sha256=candidate_tree_digest),
            _domain("connector_jobs", [], source_snapshot_identity=migration["source_snapshot_identity"], target_artifact_sha256=candidate_tree_digest),
        ],
    }
    coverage = _private_json(tmp_path / "domain-coverage.json", coverage_value)
    if indexes == "ready":
        index_items = [{
            "id": "knowledge-search",
            "status": "ready",
            "input_sha256": "sha256:" + "c" * 64,
            "artifact_sha256": "sha256:" + "d" * 64,
        }]
        absence = None
    else:
        index_items = []
        absence = {"no_indexable_objects": True, "source_enumerated": True}
    index_value = {
        "format": readiness.INDEX_READINESS_FORMAT,
        "producer": "puddingknowledge",
        "installation_id": manifest["plan"]["installation_id"],
        "source_revision": manifest["plan"]["source_revision"],
        "domain_coverage_sha256": _digest(coverage.read_bytes()),
        "candidate_tree_sha256": candidate_tree_digest,
        "state": indexes,
        "indexes": index_items,
        "absence_attestation": absence,
    }
    return {
        "migration_receipt": migration_receipt,
        "candidate": candidate,
        "credential_rebind_receipt": _private_json(tmp_path / "credential-rebind.json", _credential_receipt()),
        "domain_coverage": coverage,
        "index_readiness": _private_json(tmp_path / "index-readiness.json", index_value),
        "output": tmp_path / "cutover-readiness.json",
    }


def _build(paths: dict[str, Path]):
    return readiness.build_cutover_readiness(**paths)


@pytest.mark.parametrize("index_state", ["ready", "explicit_absent"])
def test_complete_readiness_is_path_free_inactive_and_idempotent(tmp_path: Path, index_state: str) -> None:
    paths = _inputs(tmp_path, indexes=index_state)
    result = _build(paths)

    assert result["format"] == readiness.FORMAT
    assert result["state"] == "verified_inactive_complete"
    assert result["covered_domains"] == ["session_harness", "knowledge_catalog", "connector_jobs"]
    assert result["pending_domains"] == []
    assert result["cutover_readiness_verified"] is True
    assert result["complete_migration_evidence"] is True
    assert result["writer_fence_verified"] is False
    assert result["activation_allowed"] is False
    assert all(item["mapping_coverage_bps"] == 10000 for item in result["domains"])
    assert [item["source_producer"] for item in result["domains"]] == ["puddingclaw"] * 3
    assert [item["target_producer"] for item in result["domains"]] == [
        "puddingharness", "puddingknowledge", "puddingknowledge"
    ]
    assert all(item["target_artifact_sha256"].startswith("sha256:") for item in result["domains"])
    assert result["indexes"]["state"] == index_state
    assert result["credentials"]["absent_count"] == 5
    assert len(result["credential_rebinds"]) == len(credential_rebind._SLOT_NAMES)
    assert len(result["id_resource_mappings"]) == 1
    assert result["id_resource_mappings"][0]["resource_uri"].startswith("knowledge://")
    for domain in result["domains"]:
        mappings = [
            {"source_id": item["source_id"], "resource_uri": item["resource_uri"]}
            for item in result["id_resource_mappings"]
            if item["domain"] == domain["domain"]
        ]
        assert len(mappings) == domain["mapped_count"]
        assert _digest(mappings) == domain["mapping_sha256"]
    assert json.loads(paths["output"].read_text()) == result
    assert _build(paths) == result
    encoded = json.dumps(result, sort_keys=True)
    assert str(tmp_path) not in encoded and "/legacy/home" not in encoded and "/target/vault" not in encoded


@pytest.mark.parametrize("mutation", [
    "missing_domain", "count_mismatch", "mapping_gap", "zero_without_attestation",
    "wrong_candidate_binding", "wrong_installation", "index_pending", "index_unbound",
    "absent_without_attestation", "untrusted_producer", "inventory_digest_mismatch",
    "credential_absent_unproven", "credential_uncovered", "credential_failed", "credential_skipped",
    "credential_wrong_source_digest", "credential_unrecognized_retained",
])
def test_incomplete_or_cross_installation_evidence_is_rejected(tmp_path: Path, mutation: str) -> None:
    paths = _inputs(tmp_path)
    coverage = json.loads(paths["domain_coverage"].read_text())
    indexes = json.loads(paths["index_readiness"].read_text())
    credentials = json.loads(paths["credential_rebind_receipt"].read_text())
    if mutation == "missing_domain":
        coverage["domains"].pop()
    elif mutation == "count_mismatch":
        coverage["domains"][1]["target"]["inventory"].append("asset:extra")
    elif mutation == "mapping_gap":
        coverage["domains"][1]["mapping"] = []
    elif mutation == "zero_without_attestation":
        coverage["domains"][0]["zero_object_attestation"] = None
    elif mutation == "wrong_candidate_binding":
        coverage["candidate_manifest_sha256"] = "sha256:" + "2" * 64
    elif mutation == "wrong_installation":
        coverage["installation_id"] = "another-installation"
    elif mutation == "index_pending":
        indexes["state"] = "pending"
    elif mutation == "index_unbound":
        indexes["domain_coverage_sha256"] = "sha256:" + "3" * 64
    elif mutation == "absent_without_attestation":
        indexes.update(state="explicit_absent", indexes=[], absence_attestation=None)
    elif mutation == "untrusted_producer":
        coverage["domains"][0]["source"]["producer"] = "handwritten"
    elif mutation == "inventory_digest_mismatch":
        coverage["domains"][1]["source"]["inventory_sha256"] = "sha256:" + "4" * 64
    elif mutation == "credential_absent_unproven":
        credentials["rebinds"][0].update(status="absent", materialized=False)
        credentials["counts"].update(rebound=0, absent=credentials["counts"]["absent"] + 1)
    elif mutation == "credential_uncovered":
        entry = next(item for item in credentials["rebinds"] if item["status"] == "not-applicable")
        entry["covered_by"] = ["missing-slot"]
    elif mutation == "credential_failed":
        credentials.update(state="failed", credential_continuity_verified=False)
    elif mutation == "credential_skipped":
        credentials["skipped"] = [{"artifact": "flow", "status": "skipped"}]
        credentials["counts"]["skipped"] = 1
    elif mutation == "credential_wrong_source_digest":
        credentials["rebinds"][0]["source_ref_digest"] = "sha256:" + "5" * 64
    elif mutation == "credential_unrecognized_retained":
        credentials["retained_source_refs"] = [{"ref": "unknown-credential", "reason": "unrecognized_ref"}]
        credentials["counts"]["retained"] = 1
    if mutation in {
        "missing_domain", "count_mismatch", "mapping_gap", "zero_without_attestation",
        "wrong_candidate_binding", "wrong_installation", "untrusted_producer", "inventory_digest_mismatch",
    }:
        _private_json(paths["domain_coverage"], coverage)
        indexes["domain_coverage_sha256"] = _digest(paths["domain_coverage"].read_bytes())
    _private_json(paths["index_readiness"], indexes)
    _private_json(paths["credential_rebind_receipt"], credentials)

    with pytest.raises(readiness.CutoverReadinessError):
        _build(paths)
    assert not paths["output"].exists()


def test_all_material_slots_may_be_explicitly_absent(tmp_path: Path) -> None:
    paths = _inputs(tmp_path)
    credentials = json.loads(paths["credential_rebind_receipt"].read_text())
    database = next(item for item in credentials["rebinds"] if item["slot"] == "database.password")
    database.clear()
    database.update({
        "slot": "database.password",
        "target_ref": "credential://users/local/credentials/database.password",
        "source_ref_digest": "sha256:" + "a" * 64,
        "materialized": False,
        "status": "absent",
        "source_refs": [],
    })
    credentials["counts"].update(rebound=0, absent=6)
    _private_json(paths["credential_rebind_receipt"], credentials)

    result = _build(paths)

    assert result["credentials"]["rebound_count"] == 0
    assert result["credentials"]["absent_count"] == 6
    assert all(item["status"] in {"absent", "not-applicable"} for item in result["credential_rebinds"])


@pytest.mark.parametrize("target", ["candidate_file", "candidate_manifest", "migration_receipt"])
def test_changed_partial_candidate_or_receipt_is_rejected(tmp_path: Path, target: str) -> None:
    paths = _inputs(tmp_path)
    if target == "candidate_file":
        manifest = json.loads((paths["candidate"] / "manifest.json").read_text())
        relative = next(iter(manifest["files"]))
        (paths["candidate"] / relative).write_bytes(b"tampered")
    elif target == "candidate_manifest":
        manifest_path = paths["candidate"] / "manifest.json"
        value = json.loads(manifest_path.read_text())
        value["activation_allowed"] = True
        _private_json(manifest_path, value)
    else:
        value = json.loads(paths["migration_receipt"].read_text())
        value["pending_domains"] = []
        _private_json(paths["migration_receipt"], value)
    with pytest.raises(readiness.CutoverReadinessError):
        _build(paths)
    assert not paths["output"].exists()


def test_output_publication_never_replaces_competing_receipt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = _inputs(tmp_path)
    original = readiness.os.link

    def race(source, destination):
        Path(destination).write_bytes(b"competing")
        Path(destination).chmod(0o600)
        original(source, destination)

    monkeypatch.setattr(readiness.os, "link", race)
    with pytest.raises(FileExistsError):
        _build(paths)
    assert paths["output"].read_bytes() == b"competing"


def test_cli_failure_is_redacted_and_does_not_publish(tmp_path: Path) -> None:
    paths = _inputs(tmp_path)
    paths["credential_rebind_receipt"].write_text('{"secret":"must-not-appear"}')
    paths["credential_rebind_receipt"].chmod(0o600)
    env = {"PATH": os.environ["PATH"], "PYTHONPATH": str(Path(__file__).parents[1])}
    command = [sys.executable, "-m", "knowledge_platform.distribution.cutover_readiness"]
    for key, flag in (
        ("migration_receipt", "--migration-receipt"), ("candidate", "--candidate"),
        ("credential_rebind_receipt", "--credential-rebind-receipt"),
        ("domain_coverage", "--domain-coverage"), ("index_readiness", "--index-readiness"),
        ("output", "--output"),
    ):
        command.extend((flag, str(paths[key])))
    completed = subprocess.run(command, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=20)
    assert completed.returncode == 1
    assert "must-not-appear" not in completed.stdout + completed.stderr
    assert str(tmp_path) not in completed.stdout + completed.stderr
    assert not paths["output"].exists()
