"""Build a prepared, local-only installation migration manifest.

This is a precondition builder, not an installer.  It verifies the published
local Platform snapshot at the file level, binds the manifest to its digest,
and consumes a digest-verified object inventory from the local Catalog stage.
It never reads credential values, copies files, changes an active revision, or
starts a process.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from knowledge_platform.distribution.local_catalog_inventory import (
    LocalCatalogInventoryError,
    read_local_catalog_inventory,
)

_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_STAGE_REPORT = _ROOT / "artifacts/phase0b-local-catalog/local-catalog-stage-report.json"
_DEFAULT_OUTPUT = _ROOT / "artifacts/phase0b-local-catalog/phase10-local-installation-migration-manifest.json"
_SCHEMA = _ROOT / "docs/knowledge-platform/installation-migration-manifest.schema.json"
_BACKUP_SCHEMA = _ROOT / "docs/knowledge-platform/platform-backup.schema.json"
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_RELATIVE_RE = re.compile(r"^(?!/)(?!.*(?:^|/)\.\.(?:/|$))[A-Za-z0-9._/-]+$")
_SECRET_RE = re.compile(r"(?:password|secret|token|authorization|api[_ -]?key|private[_ -]?key)\s*[:=]", re.I)


class LocalMigrationManifestError(ValueError):
    """Raised when the local snapshot cannot safely seed a manifest."""


def _digest(value: Any) -> str:
    # Match Node JSON.stringify used by the Deploy CLI backup manifest:
    # preserve insertion order, emit UTF-8 text, and omit insignificant space.
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LocalMigrationManifestError(f"{label} is not readable JSON") from error
    if not isinstance(value, dict):
        raise LocalMigrationManifestError(f"{label} must be an object")
    return value


def _assert_digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _DIGEST_RE.fullmatch(value):
        raise LocalMigrationManifestError(f"{label} is not a sha256 digest")
    return value


def _backup_manifest_projection(manifest: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in manifest.items() if key != "manifest_digest"}


def _validate_backup(backup_dir: Path) -> tuple[dict[str, Any], str, int]:
    if not backup_dir.is_absolute() or backup_dir.is_symlink() or not backup_dir.is_dir():
        raise LocalMigrationManifestError("backup directory must be an absolute real directory")
    manifest = _read_object(backup_dir / "manifest.json", "backup manifest")
    Draft202012Validator(_read_object(_BACKUP_SCHEMA, "backup schema")).validate(manifest)
    if (
        manifest.get("schema_version") != 1
        or manifest.get("format") != "agent-knowledge-platform-backup/v1"
        or manifest.get("status") != "snapshot_created"
        or manifest.get("owner") != "puddingknowledge"
        or manifest.get("activation_allowed") is not False
        or manifest.get("execution_allowed") is not False
        or manifest.get("secret_bytes_copied") is not False
        or manifest.get("source_data_mutation") is not False
        or manifest.get("legacy_claw_mutation") is not False
    ):
        raise LocalMigrationManifestError("backup manifest boundary is invalid")
    manifest_digest = _assert_digest(manifest.get("manifest_digest"), "backup manifest digest")
    if manifest_digest != _digest(_backup_manifest_projection(manifest)):
        raise LocalMigrationManifestError("backup manifest digest mismatch")
    files = manifest.get("files")
    if not isinstance(files, dict) or "platform.json" not in files:
        raise LocalMigrationManifestError("backup manifest file set is incomplete")
    entries = [path for path in backup_dir.rglob("*") if path.name != "manifest.json"]
    actual = {
        path.relative_to(backup_dir).as_posix()
        for path in entries
        if path.is_file() and not path.is_symlink()
    }
    if any(path.is_symlink() or not path.is_file() for path in entries):
        raise LocalMigrationManifestError("backup contains symlink or non-regular file")
    declared = set(files)
    if actual != declared or not all(_RELATIVE_RE.fullmatch(item) for item in declared):
        raise LocalMigrationManifestError("backup file set is not exact and portable")
    for relative, descriptor in files.items():
        if not isinstance(descriptor, dict):
            raise LocalMigrationManifestError("backup file descriptor is invalid")
        content = (backup_dir / relative).read_bytes()
        if _SECRET_RE.search(content.decode("utf-8", errors="replace")):
            raise LocalMigrationManifestError("backup contains secret-bearing bytes")
        if descriptor.get("bytes") != len(content) or descriptor.get("sha256") != _digest_bytes(content):
            raise LocalMigrationManifestError("backup file digest mismatch")
    return manifest, manifest_digest, len(files)


def _digest_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def build_manifest(*, backup_dir: Path, stage_report: Path) -> dict[str, Any]:
    # Do not resolve before validation: a symlink supplied by the caller must
    # remain observable and rejected as it is by the Deploy CLI.
    backup, backup_digest, file_count = _validate_backup(backup_dir.expanduser())
    try:
        inventory = read_local_catalog_inventory(stage_report.expanduser().absolute())
    except LocalCatalogInventoryError as error:
        raise LocalMigrationManifestError(str(error)) from error
    assets, collections = inventory.asset_count, inventory.collection_count
    source_id = _digest(
        {
            "kind": "puddingclaw-local-shadow",
            "backup": backup_digest,
            "catalog_database": inventory.database_digest,
            "catalog_objects": inventory.object_digest,
        }
    )
    namespace = "local-shadow-" + backup_digest.removeprefix("sha256:")[:16]
    manifest: dict[str, Any] = {
        "format": "agent-knowledge-platform-installation-migration/v1",
        "source": {
            "installation_id": source_id,
            "schema_revision": "claw-local-shadow-v1",
            "catalog_revision": inventory.database_digest,
        },
        "targets": {
            "puddingknowledge": "local-shadow-v1",
            "puddingharness": "local-shadow-v1",
        },
        "object_summaries": [
            {"domain": "session_harness", "object_count": 0, "source_digest": _digest({"domain": "session_harness", "count": 0})},
            {"domain": "knowledge_catalog", "object_count": assets + collections, "source_digest": inventory.object_digest},
            {"domain": "connector_jobs", "object_count": 0, "source_digest": _digest({"domain": "connector_jobs", "count": 0})},
        ],
        "id_resource_mappings": [{"source_id": "catalog-shadow", "resource_uri": "knowledge://catalog/shadow"}],
        "credential_rebinds": [{
            "slot": "knowledge-database",
            "source_ref_digest": _digest({"kind": "opaque-local-credential-slot", "slot": "knowledge-database"}),
            "target_ref": "credential://local-shadow/knowledge-database",
            "status": "pending",
        }],
        "active_writers": {
            "session_harness": "puddingclaw",
            "knowledge_catalog": "puddingclaw",
            "connector_jobs": "puddingclaw",
        },
        "checkpoint": {
            "source_snapshot": "validated-local-backup",
            "target_import": "not-started",
        },
        "rollback_strategy": "snapshot_restore",
        "state": "PREPARED",
        "snapshot_digest": backup_digest,
        "staging_namespace": namespace,
        "active_installation_revision": None,
        "started_at": "local-shadow",
        "completed_at": None,
        "rollback_window_open": True,
    }
    Draft202012Validator(_read_object(_SCHEMA, "migration schema")).validate(manifest)
    return {
        "status": "PHASE10_LOCAL_INSTALLATION_MIGRATION_MANIFEST_PREPARED_NOT_ACTIVATABLE",
        "manifest": manifest,
        "backup_file_count": file_count,
        "local_stage_counts": {"assets": assets, "collections": collections},
        "local_catalog_database_digest": inventory.database_digest,
        "local_catalog_object_digest": inventory.object_digest,
        "execution_allowed": False,
        "activation_allowed": False,
        "physical_copy_performed": False,
        "secret_bytes_read": False,
        "source_home_changed": False,
        "canonical_catalog_changed": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup-dir", type=Path, required=True)
    parser.add_argument("--stage-report", type=Path, default=_DEFAULT_STAGE_REPORT)
    parser.add_argument("--output", type=Path, default=_DEFAULT_OUTPUT)
    args = parser.parse_args()
    result = build_manifest(backup_dir=args.backup_dir, stage_report=args.stage_report)
    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result["manifest"], ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"status": result["status"], "report": str(output), "backup_file_count": result["backup_file_count"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
