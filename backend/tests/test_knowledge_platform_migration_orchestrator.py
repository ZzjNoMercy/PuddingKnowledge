from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import Column, DateTime, MetaData, String, Table, create_engine, insert, select


class _Drain:
    def __init__(self) -> None:
        self.events: list[str] = []

    def enter(self, *, installation_id: str, reason: str) -> None:
        self.events.append(f"enter:{installation_id}:{reason}")

    def assert_drained(self) -> None:
        self.events.append("assert_drained")

    def exit(self) -> None:
        self.events.append("exit")


def _slice(source, target, **kwargs):
    from knowledge_platform.catalog import RehearsalReport, build_table_snapshot

    table = Table("orchestrator_probe", MetaData(), Column("id", String(80), primary_key=True), Column("value", String(80)))
    table.create(target, checkfirst=True)
    rows = [dict(row) for row in source.execute(select(_SOURCE_TABLE)).mappings()]
    target.execute(insert(table), rows)
    snapshot = build_table_snapshot("orchestrator_probe", rows, primary_key_fields=("id",))
    return (
        RehearsalReport(
            source_revision=kwargs["source_revision"],
            target_revision=kwargs["target_revision"],
            active_revision_before=kwargs["active_revision"],
            active_revision_after=kwargs["active_revision"],
            source_tables=(snapshot,),
            target_tables=(snapshot,),
            retry_idempotent=True,
            checks={
                "secret_redaction": True,
                "file_reachability": True,
                "lease_state": True,
                "foreign_keys": True,
            },
        ),
        (("probe", "row_1", "knowledge://probe/row_1"),),
    )


_SOURCE_TABLE = Table(
    "orchestrator_source", MetaData(), Column("id", String(80), primary_key=True), Column("value", String(80))
)
_WORKER_TABLE = Table(
    "worker_access_logs",
    MetaData(),
    Column("id", String(80), primary_key=True),
    Column("key_id", String(80), nullable=False),
    Column("key_name", String(80), nullable=False),
    Column("query", String(200), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)


def _engines():
    source = create_engine("sqlite:///:memory:")
    platform = create_engine("sqlite:///:memory:")
    harness = create_engine("sqlite:///:memory:")
    _SOURCE_TABLE.create(source)
    _WORKER_TABLE.create(source)
    Table(
        "orchestrator_probe",
        MetaData(),
        Column("id", String(80), primary_key=True),
        Column("value", String(80)),
    ).create(platform)
    with source.begin() as connection:
        connection.execute(insert(_SOURCE_TABLE), {"id": "row_1", "value": "portable"})
        connection.execute(
            insert(_WORKER_TABLE),
            {
                "id": "wal_1",
                "key_id": "key_1",
                "key_name": "test",
                "query": "select 1",
                "created_at": datetime(2026, 9, 3, tzinfo=timezone.utc),
            },
        )
    return source, platform, harness


def test_catalog_migration_orchestrator_proves_drain_two_targets_and_manifest() -> None:
    from jsonschema import Draft202012Validator

    from knowledge_platform.catalog import CatalogSlice, run_catalog_migration_rehearsal

    source, platform, harness = _engines()
    drain = _Drain()
    manifest = run_catalog_migration_rehearsal(
        source,
        platform,
        harness,
        installation_id="install-1",
        source_revision="legacy-1",
        target_revision="platform-1",
        active_revision="legacy-active-1",
        drain_controller=drain,
        slices=(CatalogSlice("probe", _slice),),
    )
    assert manifest.state == "VERIFIED"
    assert manifest.active_revision_changed is False
    assert manifest.source_before_digest == manifest.source_after_digest
    assert manifest.platform_target_digest != manifest.source_before_digest
    assert manifest.harness_target_digest != manifest.source_before_digest
    assert drain.events == ["enter:install-1:catalog migration rehearsal", "assert_drained", "exit"]
    schema = json.loads(
        (Path(__file__).resolve().parents[2] / "docs/knowledge-platform/catalog-migration-manifest.schema.json").read_text()
    )
    assert list(Draft202012Validator(schema).iter_errors(manifest.to_dict())) == []
    source.dispose()
    platform.dispose()
    harness.dispose()


@pytest.mark.parametrize("checkpoint", ["after_drain", "after_schema", "after_copy", "before_verify"])
def test_catalog_migration_orchestrator_rolls_back_both_targets_after_failure(checkpoint: str) -> None:
    from knowledge_platform.catalog import CatalogSlice, MigrationInjectedFailure, run_catalog_migration_rehearsal

    source, platform, harness = _engines()
    drain = _Drain()
    with pytest.raises(MigrationInjectedFailure, match=checkpoint):
        run_catalog_migration_rehearsal(
            source,
            platform,
            harness,
            installation_id="install-1",
            source_revision="legacy-1",
            target_revision="platform-1",
            active_revision="legacy-active-1",
            drain_controller=drain,
            slices=(CatalogSlice("probe", _slice),),
            failure_checkpoint=checkpoint,
        )
    with platform.connect() as connection:
        assert connection.exec_driver_sql(
            "SELECT COUNT(*) FROM orchestrator_probe"
        ).scalar_one() == 0
    with harness.connect() as connection:
        assert connection.exec_driver_sql(
            "SELECT COUNT(*) FROM worker_access_logs"
        ).scalar_one() == 0
    assert drain.events[-1] == "exit"
    source.dispose()
    platform.dispose()
    harness.dispose()


def test_default_catalog_slices_follow_unique_ownership_order() -> None:
    from knowledge_platform.catalog import default_catalog_slices

    assert [item.name for item in default_catalog_slices(installation_id="install-1")] == [
        "core",
        "connector",
        "credential",
        "read_later",
        "structured_asset",
        "query_result",
        "processing_job",
        "authoring_job",
        "database_source",
        "notification_event",
    ]


def test_local_catalog_copy_drain_holds_a_sqlite_writer_fence(tmp_path) -> None:
    import sqlite3

    from scripts.phase0b_local_catalog_copy import _LocalSQLiteDrain

    source = tmp_path / "catalog.sqlite3"
    connection = sqlite3.connect(source)
    connection.execute("CREATE TABLE probe (id INTEGER PRIMARY KEY)")
    connection.commit()
    drain = _LocalSQLiteDrain(source)
    drain.enter(installation_id="local", reason="test")
    try:
        drain.assert_drained()
        blocked_writer = sqlite3.connect(source, timeout=0.01, isolation_level=None)
        try:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                blocked_writer.execute("BEGIN IMMEDIATE")
        finally:
            blocked_writer.close()
    finally:
        drain.exit()
        connection.close()
    assert drain.events[1]["mode"] == "sqlite-begin-immediate"
