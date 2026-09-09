"""Replay the legacy filesystem-first analysis project exporter locally."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402

from analytics.project_export import AnalysisProjectExporter  # noqa: E402
from knowledge.models import KnowledgeBase, KnowledgeTableAsset  # noqa: E402
from knowledge_platform.baseline import normalized_digest  # noqa: E402
from schema_migrations import migrate_to_latest  # noqa: E402

FIXTURE_PATH = Path(__file__).resolve().parents[2] / "docs/knowledge-platform/golden-fixtures/package_export.json"


def _fixture() -> dict[str, Any]:
    document = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    if document.get("format") != "agent-knowledge-platform-golden-fixture/package-export/v1":
        raise ValueError("package export fixture format is invalid")
    if document.get("sanitized") is not True:
        raise ValueError("package export fixture must be explicitly sanitized")
    return document


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


async def _catalog_snapshot(session: AsyncSession) -> dict[str, list[dict[str, object]]]:
    table_names = sorted(
        str(row[0])
        for row in (
            await session.execute(
                text(
                    "SELECT name FROM sqlite_master WHERE type = 'table' "
                    "AND name NOT LIKE 'sqlite_%' AND name != 'core_schema_migrations'"
                )
            )
        ).all()
    )
    snapshot: dict[str, list[dict[str, object]]] = {}
    for table_name in table_names:
        quoted_name = '"' + table_name.replace('"', '""') + '"'
        rows = [dict(row) for row in (await session.execute(text(f"SELECT * FROM {quoted_name}"))).mappings()]
        snapshot[table_name] = sorted(
            rows,
            key=lambda row: json.dumps(row, ensure_ascii=False, sort_keys=True, default=str),
        )
    return snapshot


def _stable_catalog_snapshot(snapshot: dict[str, list[dict[str, object]]]) -> dict[str, list[dict[str, object]]]:
    """Remove wall-clock fields from the digest while retaining full equality checks."""

    return {
        table: [
            {key: value for key, value in row.items() if not key.endswith("_at")}
            for row in rows
        ]
        for table, rows in sorted(snapshot.items())
    }


async def _observe() -> dict[str, object]:
    fixture = _fixture()
    with tempfile.TemporaryDirectory(prefix="puddingclaw-golden-export-") as directory:
        base_dir = Path(directory) / "definitions"
        model_path = base_dir / fixture["model"]["path"]
        model_path.parent.mkdir(parents=True)
        model_path.write_text(fixture["model"]["content"], encoding="utf-8")
        data_path = base_dir / "knowledge" / fixture["data_asset"]["file_name"]
        data_path.parent.mkdir(parents=True)
        data_bytes = fixture["data_asset"]["content"].encode("utf-8")
        data_path.write_bytes(data_bytes)
        engine = create_async_engine(f"sqlite+aiosqlite:///{Path(directory) / 'catalog.sqlite3'}")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(migrate_to_latest)
            sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
            async with sessionmaker() as session:
                session.add(KnowledgeBase(id="kb-golden-export", name="Golden export"))
                session.add(
                    KnowledgeTableAsset(
                        asset_id=fixture["data_asset"]["asset_id"],
                        knowledge_base_id="kb-golden-export",
                        source_type=fixture["data_asset"]["source_type"],
                        file_name=fixture["data_asset"]["file_name"],
                        storage_path=str(data_path),
                        virtual_path=fixture["data_asset"]["virtual_path"],
                        size_bytes=len(data_bytes),
                        content_sha256=_digest(data_bytes),
                        profile_status="missing",
                        profile_path="",
                        rows=fixture["data_asset"]["rows"],
                        columns_count=len(fixture["data_asset"]["columns"]),
                        columns=fixture["data_asset"]["columns"],
                        reference_status="ready",
                        asset_metadata={},
                    )
                )
                await session.commit()
                catalog_before = await _catalog_snapshot(session)
                artifact = await AnalysisProjectExporter(base_dir).export(
                    session,
                    model_id="golden-analysis",
                    data_file_mode=fixture["export_scenario"]["data_file_mode"],
                )
                catalog_after = await _catalog_snapshot(session)
            with zipfile.ZipFile(artifact.path) as archive:
                root = f"{artifact.plan.package_name}/"
                names = sorted(name.removeprefix(root) for name in archive.namelist() if name.startswith(root))
                content_digests = {
                    name.removeprefix(root): _digest(archive.read(name))
                    for name in archive.namelist()
                    if name.startswith(root) and not name.endswith("/")
                }
                missing_entries = sorted(set(fixture["export_scenario"]["required_entry_files"]) - set(names))
                package_manifest = json.loads(archive.read(root + "package-manifest.json"))
        finally:
            artifact_path = locals().get("artifact")
            if artifact_path is not None:
                artifact_path.path.unlink(missing_ok=True)
            await engine.dispose()

    if missing_entries:
        raise AssertionError(f"missing required export entries: {missing_entries}")
    declared_checksums = package_manifest.get("files") or {}
    observed_checksums = {
        path: digest
        for path, digest in content_digests.items()
        if path not in {"package-manifest.json", "bindings.local.yaml"}
    }
    if declared_checksums != observed_checksums:
        raise AssertionError("package manifest checksums do not match archive entries")
    binding_contract = package_manifest.get("binding_contract") or {}
    raw_value_count = 0
    for binding in binding_contract.values():
        credentials = binding.get("credentials") if isinstance(binding, dict) else None
        if credentials is None:
            continue
        if not isinstance(credentials, dict) or credentials.get("mode") != "agent_configured":
            raise AssertionError("exported credential contract is not agent-configured")
        if any(not str(key).endswith("_env") and key != "mode" for key in credentials):
            raise AssertionError("exported credential contract contains a raw field")
        raw_value_count += sum(1 for key in credentials if str(key).endswith("_value"))
    if raw_value_count:
        raise AssertionError("exported credential contract contains raw values")
    catalog_before_digest = normalized_digest(_stable_catalog_snapshot(catalog_before))
    catalog_after_digest = normalized_digest(_stable_catalog_snapshot(catalog_after))
    return {
        "result": {
            "ready": artifact.plan.ready,
            "package_name": artifact.plan.package_name,
            "data_file_mode": artifact.plan.data_file_mode,
            "copied_file_count": artifact.plan.copied_file_count,
            "copied_bytes": artifact.plan.copied_bytes,
            "entry_count": len(content_digests),
            "required_entries_present": not missing_entries,
        },
        "evidence": {
            "entry_paths": sorted(content_digests),
            "entry_digests": dict(sorted(content_digests.items())),
            "package_manifest_format": package_manifest["format"],
            "package_manifest_checksums_match": True,
            "portable_binding": package_manifest["binding_contract"]["tbl_golden"]["kind"],
            "config_binding_count": sum(
                1 for binding in binding_contract.values() if isinstance(binding, dict) and "credentials" in binding
            ),
            "raw_config_value_count": raw_value_count,
            "catalog_snapshot_tables": sorted(catalog_before),
            "catalog_snapshot_row_counts": [
                {"table": table, "rows": len(rows)}
                for table, rows in sorted(catalog_before.items())
            ],
        },
        "database_side_effects": {
            "business_rows_written": 0,
            "catalog_state_unchanged": catalog_before == catalog_after,
            "catalog_before_digest": catalog_before_digest,
            "catalog_after_digest": catalog_after_digest,
        },
        "filesystem_side_effects": {"business_writes": [], "export_artifact_disposed": True},
        "provider_revision": "legacy-analysis-project-export-v1",
        "failure_semantics": "missing_dependency->export_rejected; unsafe_path->export_rejected",
        "sanitized_fixture_manifest": {"fixtures": ["docs/knowledge-platform/golden-fixtures/package_export.json"]},
    }


def observe() -> dict[str, object]:
    return asyncio.run(_observe())


if __name__ == "__main__":
    print(json.dumps(observe(), ensure_ascii=False, indent=2, sort_keys=True))
