from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
from pathlib import Path

import pytest
from jsonschema import validate

from scripts.phase10_local_migration_manifest import (
    LocalMigrationManifestError,
    build_manifest,
)

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT.parent / "packages/knowledge-platform-deploy-cli/src/cli.mjs"
SCHEMA = ROOT.parent / "docs/knowledge-platform/installation-migration-manifest.schema.json"


def _node_backup(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    backup = tmp_path / "backup"
    subprocess.run(["node", str(CLI), "init", "--home", str(home), "--json"], check=True, capture_output=True, text=True)
    subprocess.run(
        ["node", str(CLI), "backup", "--home", str(home), "--output", str(backup), "--apply", "--json"],
        check=True,
        capture_output=True,
        text=True,
    )
    return backup


def _stage_report(path: Path, *, assets: int = 45, collections: int = 1) -> None:
    database = path.parent / "knowledge-platform.sqlite3"
    if assets >= 0 and collections >= 0:
        connection = sqlite3.connect(database)
        try:
            connection.executescript(
                "CREATE TABLE knowledge_assets (id TEXT PRIMARY KEY);"
                "CREATE TABLE knowledge_datasets (id TEXT PRIMARY KEY);"
            )
            connection.executemany("INSERT INTO knowledge_assets(id) VALUES (?)", ((f"asset-{index:05d}",) for index in range(assets)))
            connection.executemany(
                "INSERT INTO knowledge_datasets(id) VALUES (?)", ((f"dataset-{index:05d}",) for index in range(collections))
            )
            connection.commit()
        finally:
            connection.close()
        digest = "sha256:" + hashlib.sha256(database.read_bytes()).hexdigest()
        files = {"knowledge-platform.sqlite3": {"bytes": database.stat().st_size, "sha256": digest}}
    else:
        digest = "sha256:" + "0" * 64
        files = {}
    path.write_text(
        json.dumps({"targets": {"platform": {"table_counts": {
            "knowledge_assets": assets,
            "knowledge_datasets": collections,
        }, "files": files, "sha256": digest}}}),
        encoding="utf-8",
    )


def test_builder_matches_deploy_backup_digest_and_emits_prepared_manifest(tmp_path: Path) -> None:
    backup = _node_backup(tmp_path)
    stage_report = tmp_path / "stage.json"
    _stage_report(stage_report)

    result = build_manifest(backup_dir=backup, stage_report=stage_report)
    manifest = result["manifest"]
    validate(manifest, json.loads(SCHEMA.read_text(encoding="utf-8")))

    backup_manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
    assert result["status"] == "PHASE10_LOCAL_INSTALLATION_MIGRATION_MANIFEST_PREPARED_NOT_ACTIVATABLE"
    assert manifest["state"] == "PREPARED"
    assert manifest["snapshot_digest"] == backup_manifest["manifest_digest"]
    assert manifest["rollback_window_open"] is True
    assert manifest["active_installation_revision"] is None
    assert result["local_stage_counts"] == {"assets": 45, "collections": 1}
    assert manifest["source"]["catalog_revision"] == result["local_catalog_database_digest"]
    assert manifest["object_summaries"][1]["source_digest"] == result["local_catalog_object_digest"]
    assert result["execution_allowed"] is False
    assert result["activation_allowed"] is False
    assert result["secret_bytes_read"] is False


def test_builder_rejects_tampered_backup_and_invalid_stage_counts(tmp_path: Path) -> None:
    backup = _node_backup(tmp_path)
    stage_report = tmp_path / "stage.json"
    _stage_report(stage_report)
    linked_backup = tmp_path / "linked-backup"
    linked_backup.symlink_to(backup, target_is_directory=True)
    with pytest.raises(LocalMigrationManifestError, match="absolute real directory"):
        build_manifest(backup_dir=linked_backup, stage_report=stage_report)

    (backup / "platform.json").write_text("tampered\n", encoding="utf-8")
    with pytest.raises(LocalMigrationManifestError, match="backup file digest mismatch"):
        build_manifest(backup_dir=backup, stage_report=stage_report)

    backup = _node_backup(tmp_path / "second")
    _stage_report(stage_report, assets=-1)
    with pytest.raises(LocalMigrationManifestError, match="stage counts"):
        build_manifest(backup_dir=backup, stage_report=stage_report)
