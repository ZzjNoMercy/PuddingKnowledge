"""Assemble complete, inactive cutover-readiness evidence from bounded inputs.

This command does not activate an installation or switch a writer.  It verifies
that the partial forward migration candidate, three-domain object coverage,
Knowledge credential continuity, and index readiness all describe one source
installation, then publishes a path-free receipt for the cutover coordinator.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Any

from .catalog_snapshot import _path
from .credential_rebind import FORMAT as CREDENTIAL_REBIND_FORMAT, _SLOT_NAMES, _SLOTS
from .document_migration import FORMAT as CANDIDATE_FORMAT
from .installation_migration import MIGRATION_DOMAINS
from .migrate_from_claw import FORMAT as MIGRATION_RECEIPT_FORMAT


FORMAT = "puddingknowledge-cutover-readiness/v1"
DOMAIN_COVERAGE_FORMAT = "puddingknowledge-cutover-domain-coverage/v1"
INDEX_READINESS_FORMAT = "puddingknowledge-cutover-index-readiness/v1"

_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_KEY_ID = re.compile(r"^sha256:[0-9a-f]{32}$")
_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,159}$")
_CREDENTIAL_SLOT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:*@+-]{0,159}$")
_MAX_CONTROL_FILE = 4 * 1024 * 1024
_MAX_CANDIDATE_FILE = 256 * 1024 * 1024
_MAX_CANDIDATE_TOTAL = 2 * 1024**3
_EMPTY_IDS_SHA256 = "sha256:" + hashlib.sha256(b"[]").hexdigest()
_EMPTY_BYTES_SHA256 = "sha256:" + hashlib.sha256(b"").hexdigest()
_SLOT_KINDS = {name: kind for name, _fields, kind, _selector, _covered_by in _SLOTS}
_SLOT_COVERAGE = {name: list(covered_by) for name, _fields, _kind, _selector, covered_by in _SLOTS}


class CutoverReadinessError(ValueError):
    """The supplied evidence cannot prove complete cutover readiness."""


def _encode(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _require_digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise CutoverReadinessError(f"{label} must be a sha256 digest")
    return value


def _require_token(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _TOKEN.fullmatch(value):
        raise CutoverReadinessError(f"{label} must be a portable token")
    return value


def _require_resource_uri(value: Any, domain: str) -> str:
    expected = "harness://" if domain == "session_harness" else "knowledge://"
    if (
        not isinstance(value, str)
        or not value.startswith(expected)
        or ".." in value
        or "\\" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise CutoverReadinessError("mapped target must be a portable domain Resource URI")
    return value


def _read_private(path: Path | str, limit: int = _MAX_CONTROL_FILE) -> bytes:
    raw_path = Path(path).expanduser()
    if not raw_path.is_absolute():
        raise CutoverReadinessError("readiness evidence paths must be absolute")
    safe = _path(raw_path)
    descriptor = os.open(safe, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_nlink != 1
            or before.st_mode & 0o077
            or before.st_size > limit
        ):
            raise CutoverReadinessError("readiness evidence must be a bounded private file")
        data = bytearray()
        while len(data) <= limit:
            chunk = os.read(descriptor, min(65536, limit + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_size) != (after.st_dev, after.st_ino, after.st_size):
            raise CutoverReadinessError("readiness evidence changed while it was read")
    finally:
        os.close(descriptor)
    if len(data) > limit:
        raise CutoverReadinessError("readiness evidence exceeds its byte budget")
    return bytes(data)


def _object(data: bytes, label: str) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise CutoverReadinessError(f"{label} contains a duplicate key")
            result[key] = value
        return result

    try:
        value = json.loads(data, object_pairs_hook=pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CutoverReadinessError(f"{label} is not valid JSON") from error
    if not isinstance(value, dict):
        raise CutoverReadinessError(f"{label} must be a JSON object")
    return value


def _safe_relative(value: Any) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise CutoverReadinessError("candidate contains an unsafe relative path")
    path = Path(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise CutoverReadinessError("candidate contains an unsafe relative path")
    return path.as_posix()


def _verify_partial_receipt(value: dict[str, Any], candidate_manifest_digest: str) -> None:
    expected = {
        "format", "request_digest", "source_snapshot_identity", "state", "artifacts",
        "covered_domains", "pending_domains", "activation_allowed", "installation_prepared",
        "writer_fence_verified", "credential_rebind_required",
    }
    if set(value) != expected or value.get("format") != MIGRATION_RECEIPT_FORMAT:
        raise CutoverReadinessError("migration receipt contract is invalid")
    if (
        value.get("state") != "verified_inactive_partial"
        or value.get("activation_allowed") is not False
        or value.get("installation_prepared") is not False
        or value.get("writer_fence_verified") is not False
        or value.get("credential_rebind_required") is not True
    ):
        raise CutoverReadinessError("migration receipt is not an inactive partial receipt")
    _require_digest(value.get("request_digest"), "migration request digest")
    _require_digest(value.get("source_snapshot_identity"), "source snapshot identity")
    covered = value.get("covered_domains")
    pending = value.get("pending_domains")
    if (
        not isinstance(covered, list)
        or not {"document_catalog", "document_blobs"}.issubset(covered)
        or len(covered) != len(set(covered))
        or not isinstance(pending, list)
        or not pending
        or len(pending) != len(set(pending))
        or not {"other_catalog_domains", "indexes", "knowledge_credentials"}.issubset(pending)
    ):
        raise CutoverReadinessError("migration receipt domain state is invalid")
    artifacts = value.get("artifacts")
    if not isinstance(artifacts, dict) or artifacts.get("candidate/manifest.json") != candidate_manifest_digest:
        raise CutoverReadinessError("migration receipt is not bound to the candidate manifest")
    for relative, digest in artifacts.items():
        _safe_relative(relative)
        _require_digest(digest, "migration artifact digest")


def _verify_candidate(candidate: Path | str, migration_receipt: dict[str, Any]) -> tuple[dict[str, Any], str, str]:
    raw_root = Path(candidate).expanduser()
    if not raw_root.is_absolute():
        raise CutoverReadinessError("candidate path must be absolute")
    root = _path(raw_root)
    info = root.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise CutoverReadinessError("candidate must be a private owned directory")
    manifest_raw = _read_private(root / "manifest.json")
    manifest_digest = _digest(manifest_raw)
    manifest = _object(manifest_raw, "candidate manifest")
    expected = {"format", "state", "plan", "asset_bindings", "files", "activation_allowed", "complete_installation_migration"}
    if (
        set(manifest) != expected
        or manifest.get("format") != CANDIDATE_FORMAT
        or manifest.get("state") != "verified_inactive"
        or manifest.get("activation_allowed") is not False
        or manifest.get("complete_installation_migration") is not False
        or not isinstance(manifest.get("plan"), dict)
        or not isinstance(manifest.get("asset_bindings"), dict)
        or not isinstance(manifest.get("files"), dict)
        or not manifest["files"]
    ):
        raise CutoverReadinessError("candidate manifest is invalid")
    installation_id = _require_token(manifest["plan"].get("installation_id"), "candidate installation id")
    source_revision = _require_token(manifest["plan"].get("source_revision"), "candidate source revision")
    _verify_partial_receipt(migration_receipt, manifest_digest)
    expected_artifacts = migration_receipt["artifacts"]
    verified_files: dict[str, str] = {}
    total = 0
    for raw_relative, raw_digest in manifest["files"].items():
        relative = _safe_relative(raw_relative)
        expected = _require_digest(raw_digest, "candidate file digest")
        data = _read_private(root / relative, _MAX_CANDIDATE_FILE)
        total += len(data)
        if total > _MAX_CANDIDATE_TOTAL or _digest(data) != expected:
            raise CutoverReadinessError("candidate file set failed integrity verification")
        if expected_artifacts.get("candidate/" + relative) != expected:
            raise CutoverReadinessError("migration receipt is not bound to every candidate file")
        verified_files[relative] = expected
    allowed_files = {"manifest.json", "checkpoint.json", ".migration.lock", *verified_files}
    for path in root.rglob("*"):
        safe = _path(path)
        if safe.is_file() and safe.relative_to(root).as_posix() not in allowed_files:
            raise CutoverReadinessError("candidate contains an uncommitted file")
        if safe.stat().st_mode & 0o077:
            raise CutoverReadinessError("candidate contains a non-private path")
    tree_digest = _digest(_encode({"manifest": manifest_digest, "files": verified_files}))
    return manifest, manifest_digest, tree_digest


def _verify_domain_coverage(
    value: dict[str, Any], *, installation_id: str, source_revision: str,
    source_snapshot_identity: str, migration_receipt_digest: str, candidate_manifest_digest: str,
    candidate_tree_digest: str, candidate_asset_ids: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    expected = {"format", "installation_id", "source_revision", "migration_receipt_sha256", "candidate_manifest_sha256", "domains"}
    if set(value) != expected or value.get("format") != DOMAIN_COVERAGE_FORMAT:
        raise CutoverReadinessError("domain coverage contract is invalid")
    if (
        value.get("installation_id") != installation_id
        or value.get("source_revision") != source_revision
        or value.get("migration_receipt_sha256") != migration_receipt_digest
        or value.get("candidate_manifest_sha256") != candidate_manifest_digest
    ):
        raise CutoverReadinessError("domain coverage belongs to another migration")
    domains = value.get("domains")
    if not isinstance(domains, list) or len(domains) != len(MIGRATION_DOMAINS):
        raise CutoverReadinessError("domain coverage must contain all three migration domains")
    normalized: list[dict[str, Any]] = []
    resource_mappings: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in domains:
        if not isinstance(item, dict) or set(item) != {"domain", "source", "target", "mapping", "zero_object_attestation"}:
            raise CutoverReadinessError("domain coverage entry is invalid")
        domain = item.get("domain")
        if domain not in MIGRATION_DOMAINS or domain in seen:
            raise CutoverReadinessError("domain coverage contains an unknown or duplicate domain")
        seen.add(domain)
        source, target, mappings = item.get("source"), item.get("target"), item.get("mapping")
        if (
            not isinstance(source, dict)
            or set(source) != {"format", "producer", "source_snapshot_identity", "inventory", "inventory_sha256"}
            or not isinstance(target, dict)
            or set(target) != {"format", "producer", "target_artifact_sha256", "inventory", "inventory_sha256"}
            or not isinstance(mappings, list)
        ):
            raise CutoverReadinessError("domain producer receipts or mapping evidence are invalid")
        expected_target = {
            "session_harness": ("puddingharness-cutover-domain-inventory/v1", "puddingharness"),
            "knowledge_catalog": ("puddingknowledge-cutover-domain-inventory/v1", "puddingknowledge"),
            "connector_jobs": ("puddingknowledge-cutover-domain-inventory/v1", "puddingknowledge"),
        }[domain]
        if (
            source.get("format") != "puddingclaw-cutover-domain-inventory/v1"
            or source.get("producer") != "puddingclaw"
            or source.get("source_snapshot_identity") != source_snapshot_identity
            or (target.get("format"), target.get("producer")) != expected_target
        ):
            raise CutoverReadinessError("domain inventory producer is not trusted for this domain")
        target_artifact = _require_digest(target.get("target_artifact_sha256"), "target inventory artifact")
        if domain in {"knowledge_catalog", "connector_jobs"} and target_artifact != candidate_tree_digest:
            raise CutoverReadinessError("Knowledge target inventory is not bound to the verified candidate")
        source_ids, target_ids = source.get("inventory"), target.get("inventory")
        if (
            not isinstance(source_ids, list) or not isinstance(target_ids, list)
            or any(not isinstance(item_id, str) or not _TOKEN.fullmatch(item_id) for item_id in [*source_ids, *target_ids])
            or source_ids != sorted(source_ids) or target_ids != sorted(target_ids)
            or len(source_ids) != len(set(source_ids)) or len(target_ids) != len(set(target_ids))
        ):
            raise CutoverReadinessError("producer inventory IDs must be sorted, unique portable tokens")
        source_digest, target_digest = _digest(_encode(source_ids)), _digest(_encode(target_ids))
        if source.get("inventory_sha256") != source_digest or target.get("inventory_sha256") != target_digest:
            raise CutoverReadinessError("producer inventory digest does not match the enumerated IDs")
        pairs: list[dict[str, str]] = []
        for mapping in mappings:
            if not isinstance(mapping, dict) or set(mapping) != {"source_id", "target_id", "resource_uri"}:
                raise CutoverReadinessError("object mapping entry is invalid")
            source_id = _require_token(mapping.get("source_id"), "mapped source ID")
            target_id = _require_token(mapping.get("target_id"), "mapped target ID")
            resource_uri = _require_resource_uri(mapping.get("resource_uri"), domain)
            pairs.append({"source_id": source_id, "target_id": target_id, "resource_uri": resource_uri})
        if pairs != sorted(pairs, key=lambda item: (item["source_id"], item["target_id"])):
            raise CutoverReadinessError("object mappings must be canonically ordered")
        mapped_source = [item["source_id"] for item in pairs]
        mapped_target = [item["target_id"] for item in pairs]
        if sorted(mapped_source) != source_ids or sorted(mapped_target) != target_ids or len(set(mapped_source)) != len(pairs) or len(set(mapped_target)) != len(pairs):
            raise CutoverReadinessError("domain mapping coverage is not a 100 percent bijection")
        count = len(source_ids)
        mapping_digest = _digest(
            _encode([{"source_id": item["source_id"], "resource_uri": item["resource_uri"]} for item in pairs])
        )
        attestation = item.get("zero_object_attestation")
        if count == 0:
            if any(digest != _EMPTY_IDS_SHA256 for digest in (source_digest, target_digest, mapping_digest)):
                raise CutoverReadinessError("zero-object domain uses a non-empty object-set digest")
            if attestation != {"mapping_not_required": True, "source_enumerated": True, "target_enumerated": True}:
                raise CutoverReadinessError("zero-object domain requires an explicit attestation")
        elif attestation is not None or mapping_digest == _EMPTY_IDS_SHA256:
            raise CutoverReadinessError("non-empty domain has invalid zero-object evidence")
        if domain == "knowledge_catalog" and not {f"asset:{asset_id}" for asset_id in candidate_asset_ids}.issubset(target_ids):
            raise CutoverReadinessError("Knowledge producer inventory omits verified candidate assets")
        normalized.append(
            {
                "domain": domain,
                "source_count": count,
                "target_count": count,
                "mapped_count": count,
                "source_ids_sha256": source_digest,
                "target_ids_sha256": target_digest,
                "mapping_sha256": mapping_digest,
                "mapping_coverage_bps": 10000,
                "zero_object_attested": count == 0,
                "source_producer": source["producer"],
                "source_producer_format": source["format"],
                "source_inventory_receipt_sha256": _digest(_encode(source)),
                "target_producer": target["producer"],
                "target_producer_format": target["format"],
                "target_inventory_receipt_sha256": _digest(_encode(target)),
                "target_artifact_sha256": target_artifact,
            }
        )
        resource_mappings.extend(
            {"domain": domain, "source_id": item["source_id"], "resource_uri": item["resource_uri"]}
            for item in pairs
        )
    normalized.sort(key=lambda item: MIGRATION_DOMAINS.index(item["domain"]))
    resource_mappings.sort(key=lambda item: (MIGRATION_DOMAINS.index(item["domain"]), item["source_id"]))
    return normalized, resource_mappings


def _verify_credentials(value: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, str]]]:
    expected = {
        "format", "state", "credential_continuity_verified", "owner_user_id", "source", "target",
        "slots_selected", "rebinds", "skipped", "not_applicable_artifacts",
        "unrecognized_source_artifacts", "retained_source_refs", "counts",
    }
    if (
        set(value) != expected
        or value.get("format") != CREDENTIAL_REBIND_FORMAT
        or value.get("state") != "completed"
        or value.get("credential_continuity_verified") is not True
        or value.get("unrecognized_source_artifacts") != []
    ):
        raise CutoverReadinessError("credential rebind receipt is incomplete")
    owner = _require_token(value.get("owner_user_id"), "credential owner")
    source, target = value.get("source"), value.get("target")
    if (
        not isinstance(source, dict)
        or set(source) != {"home", "authority_provider", "authority_key_id", "fingerprint", "fingerprint_unchanged"}
        or source.get("fingerprint_unchanged") is not True
        or not isinstance(source.get("fingerprint"), dict)
        or not source["fingerprint"]
        or not isinstance(target, dict)
        or set(target) != {"root"}
        or any(not isinstance(value.get(key), list) for key in ("skipped", "not_applicable_artifacts", "retained_source_refs"))
    ):
        raise CutoverReadinessError("credential source or target proof is invalid")
    _require_token(source.get("authority_provider"), "credential authority provider")
    if not isinstance(source.get("authority_key_id"), str) or not _KEY_ID.fullmatch(source["authority_key_id"]):
        raise CutoverReadinessError("credential authority key id is invalid")
    for relative, digest in source["fingerprint"].items():
        _safe_relative(relative)
        _require_digest(digest, "credential source fingerprint")
    registry_digest = source["fingerprint"].get(
        f"users/{owner}/credentials/provider-registry.enc", _EMPTY_BYTES_SHA256
    )
    selected, entries, counts = value.get("slots_selected"), value.get("rebinds"), value.get("counts")
    if (
        not isinstance(selected, list) or selected != list(_SLOT_NAMES) or len(selected) != len(set(selected))
        or not isinstance(entries, list) or len(entries) != len(selected)
        or not isinstance(counts, dict)
        or set(counts) != {"rebound", "absent", "failed", "not_applicable", "skipped", "retained"}
        or any(type(count) is not int or count < 0 for count in counts.values())
    ):
        raise CutoverReadinessError("credential rebind must enumerate every owned slot")
    statuses: dict[str, str] = {}
    rebound = absent = not_applicable = 0
    terminal: list[dict[str, str]] = []
    if [entry.get("slot") if isinstance(entry, dict) else None for entry in entries] != selected:
        raise CutoverReadinessError("credential rebind entries are not in authoritative slot order")
    for entry in entries:
        if not isinstance(entry, dict):
            raise CutoverReadinessError("credential rebind entry is invalid")
        slot = entry.get("slot")
        if not isinstance(slot, str) or not _CREDENTIAL_SLOT.fullmatch(slot):
            raise CutoverReadinessError("credential slot must be a portable slot selector")
        if slot in statuses or slot not in selected:
            raise CutoverReadinessError("credential rebind slots do not match the selected slots")
        if _require_digest(entry.get("source_ref_digest"), "credential source reference") != registry_digest:
            raise CutoverReadinessError("credential slot is not bound to the fingerprinted source registry")
        target_ref = entry.get("target_ref")
        if not isinstance(target_ref, str) or not target_ref.startswith("credential://") or ".." in target_ref or "\\" in target_ref:
            raise CutoverReadinessError("credential target reference is invalid")
        status = entry.get("status")
        if status == "rebound":
            source_refs = entry.get("source_refs")
            vault_ref = entry.get("vault_ref")
            if (
                _SLOT_KINDS[slot] == "reference"
                or entry.get("materialized") is not True
                or not isinstance(source_refs, list)
                or not source_refs
                or source_refs != sorted(source_refs)
                or len(source_refs) != len(set(source_refs))
                or any(not isinstance(ref, str) or not _TOKEN.fullmatch(ref) for ref in source_refs)
                or not isinstance(vault_ref, str)
                or not vault_ref.startswith("vault://")
                or ".." in vault_ref
                or "\\" in vault_ref
            ):
                raise CutoverReadinessError("rebound credential was not materialized")
            rebound += 1
        elif status == "absent":
            if (
                _SLOT_KINDS[slot] == "reference"
                or entry.get("materialized") is not False
                or entry.get("source_refs") != []
            ):
                raise CutoverReadinessError("absent credential lacks an explicit empty source enumeration")
            absent += 1
        elif status == "not-applicable":
            if (
                _SLOT_KINDS[slot] != "reference"
                or entry.get("materialized") is not False
                or entry.get("covered_by") != _SLOT_COVERAGE[slot]
            ):
                raise CutoverReadinessError("non-material credential slot lacks rebound coverage")
            not_applicable += 1
        else:
            raise CutoverReadinessError("credential slot is not in a verified terminal state")
        statuses[slot] = status
        terminal.append({"slot": slot, "source_ref_digest": entry["source_ref_digest"], "target_ref": target_ref, "status": status})
    if set(statuses) != set(selected):
        raise CutoverReadinessError("credential rebind slots do not match the selected slots")
    for entry in entries:
        if entry["status"] == "not-applicable" and any(statuses.get(slot) not in {"rebound", "absent"} for slot in entry["covered_by"]):
            raise CutoverReadinessError("non-material credential slot is not covered by terminal credential evidence")
    for artifact in value["not_applicable_artifacts"]:
        if (
            not isinstance(artifact, dict)
            or set(artifact) != {"artifact", "status", "reason", "source_ref_digest"}
            or artifact.get("status") != "not-applicable"
            or artifact.get("reason") not in {"harness_owned_skill_secrets", "harness_owned_provider_profile_state"}
        ):
            raise CutoverReadinessError("credential artifact disposition is not trusted")
        relative = _safe_relative(artifact.get("artifact"))
        artifact_digest = _require_digest(artifact.get("source_ref_digest"), "credential artifact source reference")
        if source["fingerprint"].get(relative) != artifact_digest:
            raise CutoverReadinessError("credential artifact is not bound to the source fingerprint")
    for retained in value["retained_source_refs"]:
        if (
            not isinstance(retained, dict)
            or set(retained) != {"ref", "reason"}
            or retained.get("reason") not in {"catalog_wave_owned", "harness_owned"}
        ):
            raise CutoverReadinessError("credential receipt retains an unowned or unrecognized reference")
        _require_token(retained.get("ref"), "retained credential reference")
    if (
        counts.get("rebound") != rebound
        or counts.get("not_applicable") != not_applicable
        or counts.get("absent") != absent
        or counts.get("failed") != 0
        or counts.get("skipped") != len(value["skipped"])
        or counts.get("retained") != len(value["retained_source_refs"])
        or value["skipped"]
    ):
        raise CutoverReadinessError("credential rebind counts are incomplete")
    return (
        {"selected_count": len(selected), "rebound_count": rebound, "absent_count": absent, "covered_reference_count": not_applicable},
        terminal,
    )


def _verify_indexes(
    value: dict[str, Any], *, installation_id: str, source_revision: str, domain_coverage_digest: str,
    candidate_tree_digest: str,
) -> dict[str, Any]:
    expected = {
        "format", "producer", "installation_id", "source_revision", "domain_coverage_sha256",
        "candidate_tree_sha256", "state", "indexes", "absence_attestation",
    }
    if set(value) != expected or value.get("format") != INDEX_READINESS_FORMAT:
        raise CutoverReadinessError("index readiness contract is invalid")
    if (
        value.get("installation_id") != installation_id
        or value.get("source_revision") != source_revision
        or value.get("domain_coverage_sha256") != domain_coverage_digest
        or value.get("producer") != "puddingknowledge"
        or value.get("candidate_tree_sha256") != candidate_tree_digest
    ):
        raise CutoverReadinessError("index producer receipt belongs to another verified candidate")
    state, indexes = value.get("state"), value.get("indexes")
    if not isinstance(indexes, list):
        raise CutoverReadinessError("index readiness entries must be a list")
    if state == "ready":
        if not indexes or value.get("absence_attestation") is not None:
            raise CutoverReadinessError("ready index evidence is empty or contradictory")
        seen: set[str] = set()
        for item in indexes:
            if not isinstance(item, dict) or set(item) != {"id", "status", "input_sha256", "artifact_sha256"}:
                raise CutoverReadinessError("index readiness entry is invalid")
            index_id = _require_token(item.get("id"), "index id")
            if index_id in seen or item.get("status") != "ready":
                raise CutoverReadinessError("index readiness contains a duplicate or non-ready index")
            seen.add(index_id)
            _require_digest(item.get("input_sha256"), "index input digest")
            _require_digest(item.get("artifact_sha256"), "index artifact digest")
        return {"state": "ready", "ready_count": len(indexes), "explicit_absence": False}
    if state == "explicit_absent":
        if indexes or value.get("absence_attestation") != {"no_indexable_objects": True, "source_enumerated": True}:
            raise CutoverReadinessError("absent indexes require an explicit zero-index attestation")
        return {"state": "explicit_absent", "ready_count": 0, "explicit_absence": True}
    raise CutoverReadinessError("indexes are neither ready nor explicitly absent")


def _publish(path: Path, data: bytes) -> None:
    if not path.is_absolute():
        raise CutoverReadinessError("readiness output path must be absolute")
    output = _path(path)
    if not output.parent.is_dir():
        raise CutoverReadinessError("readiness output parent is unavailable")
    if output.exists():
        if _read_private(output) != data:
            raise CutoverReadinessError("existing readiness receipt disagrees")
        return
    part = _path(str(output) + ".part")
    if part.exists():
        if _read_private(part) != data:
            raise CutoverReadinessError("interrupted readiness receipt disagrees")
    else:
        descriptor = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    os.link(part, output)
    part.unlink()
    directory = os.open(output.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def build_cutover_readiness(
    *, migration_receipt: Path | str, candidate: Path | str, credential_rebind_receipt: Path | str,
    domain_coverage: Path | str, index_readiness: Path | str, output: Path | str,
) -> dict[str, Any]:
    """Verify all complete migration evidence and publish one inactive receipt."""

    migration_raw = _read_private(migration_receipt)
    migration = _object(migration_raw, "migration receipt")
    candidate_manifest, candidate_manifest_digest, candidate_tree_digest = _verify_candidate(candidate, migration)
    installation_id = candidate_manifest["plan"]["installation_id"]
    source_revision = candidate_manifest["plan"]["source_revision"]

    credential_raw = _read_private(credential_rebind_receipt)
    credential_summary, credential_rebinds = _verify_credentials(_object(credential_raw, "credential rebind receipt"))
    coverage_raw = _read_private(domain_coverage)
    coverage_digest = _digest(coverage_raw)
    coverage, id_resource_mappings = _verify_domain_coverage(
        _object(coverage_raw, "domain coverage"),
        installation_id=installation_id,
        source_revision=source_revision,
        source_snapshot_identity=migration["source_snapshot_identity"],
        migration_receipt_digest=_digest(migration_raw),
        candidate_manifest_digest=candidate_manifest_digest,
        candidate_tree_digest=candidate_tree_digest,
        candidate_asset_ids=set(candidate_manifest["asset_bindings"]),
    )
    indexes_raw = _read_private(index_readiness)
    index_summary = _verify_indexes(
        _object(indexes_raw, "index readiness"),
        installation_id=installation_id,
        source_revision=source_revision,
        domain_coverage_digest=coverage_digest,
        candidate_tree_digest=candidate_tree_digest,
    )
    receipt = {
        "format": FORMAT,
        "state": "verified_inactive_complete",
        "installation_id": installation_id,
        "source_revision": source_revision,
        "source_snapshot_identity": migration["source_snapshot_identity"],
        "inputs": {
            "migration_receipt_sha256": _digest(migration_raw),
            "candidate_manifest_sha256": candidate_manifest_digest,
            "candidate_tree_sha256": candidate_tree_digest,
            "credential_rebind_receipt_sha256": _digest(credential_raw),
            "domain_coverage_sha256": coverage_digest,
            "index_readiness_sha256": _digest(indexes_raw),
        },
        "domains": coverage,
        "id_resource_mappings": id_resource_mappings,
        "credentials": credential_summary,
        "credential_rebinds": credential_rebinds,
        "indexes": index_summary,
        "covered_domains": list(MIGRATION_DOMAINS),
        "pending_domains": [],
        "cutover_readiness_verified": True,
        "complete_migration_evidence": True,
        "writer_fence_verified": False,
        "activation_allowed": False,
    }
    encoded = _encode(receipt) + b"\n"
    if len(encoded) > _MAX_CONTROL_FILE:
        raise CutoverReadinessError("cutover readiness receipt exceeds its byte budget")
    _publish(Path(output).expanduser(), encoded)
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--migration-receipt", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--credential-rebind-receipt", required=True)
    parser.add_argument("--domain-coverage", required=True)
    parser.add_argument("--index-readiness", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        result = build_cutover_readiness(
            migration_receipt=args.migration_receipt,
            candidate=args.candidate,
            credential_rebind_receipt=args.credential_rebind_receipt,
            domain_coverage=args.domain_coverage,
            index_readiness=args.index_readiness,
            output=args.output,
        )
    except Exception:
        print(json.dumps({"format": FORMAT, "state": "rejected", "error_code": "cutover_readiness_rejected", "activation_allowed": False}, sort_keys=True))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
