"""Run the legacy Catalog migration against a fixed sanitized fixture."""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any

import catalog_migration
from knowledge.models import Base
from schema_migrations import migrate_to_latest

FIXTURE_PATH = Path(__file__).resolve().parents[2] / "docs/knowledge-platform/golden-fixtures/catalog_migration_rehearsal.json"


def _fixture() -> dict[str, Any]:
    document = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    if document.get("format") != "agent-knowledge-platform-golden-fixture/catalog-migration/v1":
        raise ValueError("catalog fixture format is invalid")
    if document.get("sanitized") is not True or not isinstance(document.get("tables"), dict):
        raise ValueError("catalog fixture must be explicitly sanitized")
    return document


async def _seed_source(url: str, tables: dict[str, Any]) -> None:
    engine = catalog_migration._create_engine(url)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(migrate_to_latest)
            for table_name, rows in tables.items():
                table = Base.metadata.tables.get(table_name)
                if table is None or not isinstance(rows, list):
                    raise ValueError(f"catalog fixture contains an unsupported table: {table_name}")
                if rows:
                    hydrated_rows = [
                        {
                            column.name: catalog_migration._deserialize_value(column, row.get(column.name))
                            for column in table.c
                        }
                        for row in rows
                    ]
                    await connection.execute(table.insert(), hydrated_rows)
    finally:
        await engine.dispose()


async def _observe() -> dict[str, object]:
    fixture = _fixture()
    with tempfile.TemporaryDirectory(prefix="puddingclaw-golden-catalog-") as directory:
        source_url = f"sqlite+aiosqlite:///{Path(directory) / 'source.sqlite3'}"
        target_url = f"sqlite+aiosqlite:///{Path(directory) / 'target.sqlite3'}"
        await _seed_source(source_url, fixture["tables"])
        exported = await catalog_migration.export_catalog(source_url)
        imported = await catalog_migration.import_catalog(target_url, exported)
        verification = await catalog_migration.validate_migration(source_url, target_url)
    return {
        "result": {
            "imported_rows": imported["total_rows"],
            "verification_ok": verification["ok"],
            "schema_version": verification["schema_version"],
        },
        "evidence": {
            "tables": [
                {
                    "table": name,
                    "source_rows": detail["source_rows"],
                    "target_rows": detail["target_rows"],
                    "content_digest_match": detail["content_digest_match"],
                }
                for name, detail in verification["tables"].items()
            ],
            "sqlite_checks": verification["sqlite_checks"],
        },
        "database_side_effects": {
            "business_rows_written": imported["total_rows"],
            "active_revision_changed": False,
        },
        "filesystem_side_effects": {"business_writes": []},
        "provider_revision": "legacy-catalog-migration-v1",
        "failure_semantics": "import_failure->transaction_rollback; verify_mismatch->no_switch",
        "sanitized_fixture_manifest": {"fixtures": ["docs/knowledge-platform/golden-fixtures/catalog_migration_rehearsal.json"]},
    }


def observe() -> dict[str, object]:
    """No-argument observer consumed by ``phase0a_baseline_record.py``."""

    return asyncio.run(_observe())
