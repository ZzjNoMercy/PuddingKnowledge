from __future__ import annotations

import ast
import fnmatch
import json
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def _imports(root: Path) -> list[tuple[Path, str]]:
    if not root.exists():
        return []
    result: list[tuple[Path, str]] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix in {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}:
            text = path.read_text(encoding="utf-8")
            for match in re.finditer(
                r"(?:import\s+(?:[^;\n]*?\s+from\s+|\s*)|export\s+[^;\n]*?\s+from\s+|require(?:\.resolve)?\s*\(|import\s*\()(['\"])([^'\"]+)\1",
                text,
            ):
                result.append((path, match.group(2)))
            continue
        if path.suffix != ".py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        dynamic_import_names = {"import_module"}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "importlib":
                for alias in node.names:
                    if alias.name == "import_module":
                        dynamic_import_names.add(alias.asname or alias.name)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                result.extend((path, alias.name) for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    result.append((path, node.module))
                else:
                    # ``from . import graph`` has no ``node.module`` but still
                    # creates a dependency on the imported sibling.
                    result.extend((path, alias.name) for alias in node.names)
            elif isinstance(node, ast.Call) and (
                (
                    isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.attr == "import_module"
                )
                or (isinstance(node.func, ast.Name) and node.func.id in dynamic_import_names)
            ):
                if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                    result.append((path, node.args[0].value))
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "__import__":
                if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                    result.append((path, node.args[0].value))
    return result


def test_contracts_have_no_framework_or_product_imports() -> None:
    forbidden_roots = {
        "fastapi",
        "pydantic",
        "sqlalchemy",
        "langchain",
        "langgraph",
        "mcp",
        "graph",
        "harness",
        "tools",
        "knowledge",
        "analytics",
        "vanna",
    }
    violations = [
        (str(path.relative_to(ROOT)), module)
        for path, module in _imports(ROOT / "knowledge_contracts")
        if any(segment in forbidden_roots for segment in module.split("."))
    ]
    assert not violations, f"knowledge_contracts imports forbidden modules: {violations}"


def test_boundary_scanner_catches_relative_dynamic_and_frontend_imports(tmp_path: Path) -> None:
    package = tmp_path / "boundary"
    package.mkdir()
    (package / "relative.py").write_text("from . import graph\n", encoding="utf-8")
    (package / "dynamic.py").write_text(
        "from importlib import import_module\nfrom importlib import import_module as load\nimport_module('tools.runtime')\nload('graph.runtime')\n__import__('harness.worker')\n",
        encoding="utf-8",
    )
    (package / "qualified.py").write_text("from backend.graph import agent\n", encoding="utf-8")
    (package / "surface.ts").write_text(
        "import x from 'knowledge/client';\nconst y = require('analytics/sql');\nrequire.resolve('graph/loader');\n",
        encoding="utf-8",
    )
    imports = _imports(package)
    assert {module for _, module in imports} >= {
        "graph",
        "tools.runtime",
        "graph.runtime",
        "harness.worker",
        "backend.graph",
        "knowledge/client",
        "analytics/sql",
        "graph/loader",
    }


def test_future_platform_package_cannot_point_back_to_legacy_layers() -> None:
    forbidden_roots = {"graph", "harness", "tools", "knowledge", "analytics", "vanna"}
    violations = [
        (str(path.relative_to(ROOT)), module)
        for path, module in _imports(ROOT / "knowledge_platform")
        if any(segment in forbidden_roots for segment in module.split("."))
    ]
    assert not violations, f"knowledge_platform imports legacy layers: {violations}"


def test_catalog_ownership_uses_two_distinct_metadata_objects() -> None:
    from knowledge_platform.catalog import (
        HARNESS_METADATA,
        KNOWLEDGE_METADATA,
        CatalogOwner,
        create_session_factory,
    )

    assert HARNESS_METADATA is not KNOWLEDGE_METADATA
    assert {"harness_catalog_schema_versions", "worker_access_logs"} <= set(HARNESS_METADATA.tables)
    assert {
        "knowledge_spaces",
        "knowledge_assets",
        "knowledge_datasets",
        "knowledge_credentials",
        "knowledge_credential_grants",
        "knowledge_oauth_sessions",
        "knowledge_web_captures",
        "knowledge_structured_assets",
        "knowledge_query_results",
        "knowledge_processing_jobs",
        "knowledge_processing_events",
        "knowledge_authoring_jobs",
        "knowledge_authoring_events",
        "knowledge_database_connectors",
        "knowledge_notification_events",
        "knowledge_catalog_schema_versions",
    } <= set(KNOWLEDGE_METADATA.tables)
    engine, sessions = create_session_factory("sqlite+aiosqlite:///:memory:", owner=CatalogOwner.KNOWLEDGE)
    assert sessions.kw["expire_on_commit"] is False
    engine.sync_engine.dispose()


def test_rollback_state_digest_includes_row_values() -> None:
    from sqlalchemy import Column, MetaData, String, Table, create_engine

    from knowledge_platform.catalog.rehearsal_runner import _target_database_state

    engine = create_engine("sqlite:///:memory:")
    metadata = MetaData()
    table = Table("rollback_probe_fixture", metadata, Column("id", String, primary_key=True), Column("value", String))
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(table.insert(), {"id": "one", "value": "before"})
    before = _target_database_state(engine)
    with engine.begin() as connection:
        connection.execute(table.update().values(value="after"))
    assert _target_database_state(engine) != before
    engine.dispose()


def test_catalog_rehearsal_snapshot_redacts_secrets_and_is_deterministic() -> None:
    from knowledge_platform.catalog import build_table_snapshot

    rows = [
        {"id": "2", "content": "b", "password": "secret-b", "path": "assets/b.md", "lease": "free"},
        {"id": "1", "content": "a", "password": "secret-a", "path": "assets/a.md", "lease": "claimed"},
    ]
    left = build_table_snapshot(
        "documents",
        rows,
        primary_key_fields=("id",),
        secret_fields=("password",),
        file_reference_fields=("path",),
        lease_fields=("lease",),
    )
    right = build_table_snapshot(
        "documents",
        reversed(rows),
        primary_key_fields=("id",),
        secret_fields=("password",),
        file_reference_fields=("path",),
        lease_fields=("lease",),
    )
    assert left == right
    assert left.redacted_secret_fields == ("password",)


def test_independent_platform_catalog_migrates_without_legacy_tables() -> None:
    import asyncio

    from sqlalchemy import inspect

    from knowledge_platform.catalog import (
        SCHEMA_VERSIONS,
        CatalogOwner,
        create_session_factory,
        migrate_to_latest,
    )

    async def run() -> set[str]:
        engine, sessions = create_session_factory("sqlite+aiosqlite:///:memory:", owner=CatalogOwner.KNOWLEDGE)
        del sessions
        async with engine.begin() as connection:
            applied = await connection.run_sync(migrate_to_latest)
            assert applied == list(SCHEMA_VERSIONS)
            assert await connection.run_sync(migrate_to_latest) == []
            tables = await connection.run_sync(lambda sync_connection: set(inspect(sync_connection).get_table_names()))
        await engine.dispose()
        return tables

    tables = asyncio.run(run())
    assert {
        "knowledge_spaces",
        "knowledge_assets",
        "knowledge_datasets",
        "knowledge_credentials",
        "knowledge_credential_grants",
        "knowledge_oauth_sessions",
        "knowledge_catalog_schema_versions",
    } <= tables
    assert "knowledge_documents" not in tables
    assert "worker_access_logs" not in tables


def test_harness_catalog_migrates_independently_from_platform() -> None:
    from sqlalchemy import create_engine, inspect

    from knowledge_platform.catalog import migrate_harness_to_latest, migrate_to_latest

    harness = create_engine("sqlite:///:memory:")
    platform = create_engine("sqlite:///:memory:")
    with harness.begin() as connection:
        assert migrate_harness_to_latest(connection) == [1]
        assert migrate_harness_to_latest(connection) == []
        assert "worker_access_logs" in inspect(connection).get_table_names()
    with platform.begin() as connection:
        migrate_to_latest(connection)
        assert "worker_access_logs" not in inspect(connection).get_table_names()
    harness.dispose()
    platform.dispose()


def test_existing_platform_schema_advances_to_credential_catalog_version() -> None:
    from datetime import datetime, timezone

    from sqlalchemy import create_engine, inspect

    from knowledge_platform.catalog import (
        KnowledgeAsset,
        KnowledgeCatalogSchemaVersion,
        KnowledgeConnector,
        KnowledgeCredential,
        KnowledgeDataset,
        KnowledgeSourceItem,
        KnowledgeSpace,
        KnowledgeSyncRun,
        migrate_to_latest,
    )

    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        KnowledgeCatalogSchemaVersion.__table__.create(connection)
        for table in (
            KnowledgeSpace.__table__,
            KnowledgeAsset.__table__,
            KnowledgeDataset.__table__,
            KnowledgeConnector.__table__,
            KnowledgeSourceItem.__table__,
            KnowledgeSyncRun.__table__,
        ):
            table.create(connection)
        connection.execute(
            KnowledgeCatalogSchemaVersion.__table__.insert(),
            {"version": 1, "applied_at": datetime(2026, 9, 3, tzinfo=timezone.utc)},
        )
        assert migrate_to_latest(connection) == [2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]
        tables = set(inspect(connection).get_table_names())
    assert KnowledgeCredential.__table__.name in tables
    engine.dispose()


def test_existing_v7_platform_schema_advances_to_latest_notification_scope_version() -> None:

    from sqlalchemy import create_engine, delete, inspect

    from knowledge_platform.catalog import (
        KnowledgeCatalogSchemaVersion,
        KnowledgeDatabaseSource,
        KnowledgeNotificationEvent,
        KnowledgeNotificationEventScope,
        migrate_to_latest,
    )

    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        migrate_to_latest(connection)
        KnowledgeDatabaseSource.__table__.drop(connection)
        KnowledgeNotificationEvent.__table__.drop(connection)
        KnowledgeNotificationEventScope.__table__.drop(connection)
        connection.execute(
            delete(KnowledgeCatalogSchemaVersion.__table__).where(KnowledgeCatalogSchemaVersion.version >= 8)
        )
        assert migrate_to_latest(connection) == [8, 9, 10, 11, 12]
        assert KnowledgeDatabaseSource.__table__.name in inspect(connection).get_table_names()
        assert KnowledgeNotificationEvent.__table__.name in inspect(connection).get_table_names()
        history = connection.execute(KnowledgeCatalogSchemaVersion.__table__.select()).all()
    assert {int(row[0]) for row in history} == set(range(1, 13))
    engine.dispose()


def test_platform_migration_rejects_unknown_future_version() -> None:
    from sqlalchemy import create_engine

    from knowledge_platform.catalog import KnowledgeCatalogSchemaVersion, migrate_to_latest

    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        KnowledgeCatalogSchemaVersion.__table__.create(connection)
        connection.execute(KnowledgeCatalogSchemaVersion.__table__.insert(), {"version": 99})
        with pytest.raises(RuntimeError, match="future"):
            migrate_to_latest(connection)
    engine.dispose()


def test_platform_migration_rejects_non_contiguous_history() -> None:
    from sqlalchemy import create_engine

    from knowledge_platform.catalog import KnowledgeCatalogSchemaVersion, migrate_to_latest

    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        KnowledgeCatalogSchemaVersion.__table__.create(connection)
        connection.execute(KnowledgeCatalogSchemaVersion.__table__.insert(), {"version": 7})
        with pytest.raises(RuntimeError, match="non-contiguous"):
            migrate_to_latest(connection)
    engine.dispose()


def test_dataset_primary_key_is_versioned_not_singleton() -> None:
    from knowledge_platform.catalog import KnowledgeDataset

    primary_key = tuple(column.name for column in KnowledgeDataset.__table__.primary_key.columns)
    assert primary_key == ("space_id", "id", "version")


def test_dataset_versions_can_coexist_in_runtime_catalog() -> None:
    import asyncio

    from sqlalchemy import select

    from knowledge_platform.catalog import (
        CatalogOwner,
        KnowledgeDataset,
        create_session_factory,
        migrate_to_latest,
    )

    async def run() -> list[str]:
        engine, sessions = create_session_factory("sqlite+aiosqlite:///:memory:", owner=CatalogOwner.KNOWLEDGE)
        del sessions
        async with engine.begin() as connection:
            await connection.run_sync(migrate_to_latest)
            await connection.execute(
                KnowledgeDataset.__table__.insert(),
                [
                    {
                        "space_id": "space-1",
                        "id": "dataset-1",
                        "version": "v1",
                        "name": "Dataset",
                        "kind": "table",
                        "description": "",
                    },
                    {
                        "space_id": "space-1",
                        "id": "dataset-1",
                        "version": "v2",
                        "name": "Dataset",
                        "kind": "table",
                        "description": "",
                    },
                ],
            )
            rows = await connection.execute(
                select(KnowledgeDataset.version)
                .where(KnowledgeDataset.space_id == "space-1", KnowledgeDataset.id == "dataset-1")
                .order_by(KnowledgeDataset.version)
            )
            versions = [str(row[0]) for row in rows]
        await engine.dispose()
        return versions

    assert asyncio.run(run()) == ["v1", "v2"]


def test_catalog_rehearsal_fails_closed_on_revision_change_or_digest_mismatch() -> None:
    from dataclasses import replace

    from knowledge_platform.catalog import (
        REQUIRED_CHECKS,
        RehearsalReport,
        RehearsalVerificationError,
        build_table_snapshot,
    )

    snapshot = build_table_snapshot("documents", [{"id": "1", "body": "ok"}], primary_key_fields=("id",))
    report = RehearsalReport(
        source_revision="legacy-1",
        target_revision="platform-1",
        active_revision_before="legacy-1",
        active_revision_after="legacy-1",
        source_tables=(snapshot,),
        target_tables=(snapshot,),
        retry_idempotent=True,
        checks={check: True for check in REQUIRED_CHECKS},
    )
    report.verify_safe()
    with pytest.raises(RehearsalVerificationError, match="active_revision"):
        replace(report, active_revision_after="platform-1").verify_safe()
    changed = build_table_snapshot("documents", [{"id": "1", "body": "changed"}], primary_key_fields=("id",))
    with pytest.raises(RehearsalVerificationError, match="normalized_content_digest"):
        replace(report, target_tables=(changed,)).verify_safe()


def test_core_catalog_rehearsal_copies_redacts_and_replays_without_cutover(tmp_path: Path) -> None:
    from datetime import datetime, timezone

    from jsonschema import Draft202012Validator
    from sqlalchemy import JSON, Column, DateTime, MetaData, String, Table, Text, create_engine, select

    from knowledge_platform.catalog import (
        CoreCatalogRehearsalResult,
        KnowledgeAsset,
        KnowledgeDataset,
        run_core_catalog_rehearsal,
        run_core_catalog_rehearsal_with_rollback_probes,
    )

    source_engine = create_engine("sqlite:///:memory:")
    target_engine = create_engine("sqlite:///:memory:")
    source_metadata = MetaData()
    legacy_bases = Table(
        "knowledge_bases",
        source_metadata,
        # Keep the fixture schema explicit and independent from the legacy ORM.
        Column("id", String(64), primary_key=True),
        Column("name", String(200), nullable=False),
        Column("description", Text, nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False),
        Column("updated_at", DateTime(timezone=True), nullable=False),
    )
    legacy_documents = Table(
        "knowledge_documents",
        source_metadata,
        Column("id", String(64), primary_key=True),
        Column("knowledge_base_id", String(64), nullable=False),
        Column("title", String(300), nullable=False),
        Column("source_type", String(40), nullable=False),
        Column("source_path", Text, nullable=False),
        Column("storage_path", Text, nullable=False),
        Column("virtual_path", Text, nullable=False),
        Column("mime_type", String(120), nullable=False),
        Column("content_sha256", String(64), nullable=False),
        Column("status", String(40), nullable=False),
        Column("doc_metadata", JSON, nullable=False),
        Column("origin_url", Text, nullable=True),
        Column("created_at", DateTime(timezone=True), nullable=False),
        Column("updated_at", DateTime(timezone=True), nullable=False),
    )
    source_metadata.create_all(source_engine)
    source_file = tmp_path / "source.md"
    storage_file = tmp_path / "storage.md"
    source_file.write_text("source", encoding="utf-8")
    storage_file.write_text("source", encoding="utf-8")
    moment = datetime(2026, 9, 3, tzinfo=timezone.utc)
    with source_engine.begin() as connection:
        connection.execute(
            legacy_bases.insert(),
            {
                "id": "kb_1",
                "name": "Sales",
                "description": "Sales knowledge",
                "created_at": moment,
                "updated_at": moment,
            },
        )
        connection.execute(
            legacy_documents.insert(),
            {
                "id": "doc_1",
                "knowledge_base_id": "kb_1",
                "title": "Policy",
                "source_type": "local_markdown",
                "source_path": str(source_file),
                "storage_path": str(storage_file),
                "virtual_path": "sales/policy.md",
                "mime_type": "text/markdown",
                "content_sha256": "abc123",
                "status": "ready",
                "doc_metadata": {"owner": "sales", "api_key": "must-not-cross"},
                "origin_url": "https://example.test/policy?token=must-not-cross",
                "created_at": moment,
                "updated_at": moment,
            },
        )

    with source_engine.connect() as source:
        result = run_core_catalog_rehearsal_with_rollback_probes(
            source,
            target_engine,
            installation_id="install_1",
            source_revision="legacy-schema-1",
            target_revision="platform-rehearsal-1",
            active_revision="legacy-active-1",
            file_reference_checker=lambda reference: Path(reference).exists(),
        )
        assert isinstance(result, CoreCatalogRehearsalResult)
        assert result.report.to_dict()["active_revision_changed"] is False
        assert result.report.retry_idempotent is True
        assert all(result.report.checks.values())
        assert "lease-bearing job tables are excluded" in result.report.check_scopes["lease_state"]
        result.write_json(tmp_path / "rehearsal-report.json")
    with target_engine.connect() as target:
        target_metadata = target.execute(select(KnowledgeAsset.metadata_json)).scalar_one()
        assert target_metadata["api_key"] == "<redacted>"
        report_json = json.loads((tmp_path / "rehearsal-report.json").read_text(encoding="utf-8"))
        assert report_json["report"]["injected_failure_checkpoints"] == [
            "after_schema",
            "after_spaces",
            "after_assets",
            "after_datasets",
            "before_verification",
        ]
        assert report_json["report"]["active_revision_changed"] is False
        assert "must-not-cross" not in (tmp_path / "rehearsal-report.json").read_text(encoding="utf-8")
        assert target.execute(select(KnowledgeDataset.version)).scalar_one().startswith("legacy-")
        report_schema = json.loads(
            (ROOT.parent / "docs" / "knowledge-platform" / "catalog-rehearsal-report.schema.json").read_text(
                encoding="utf-8"
            )
        )
        Draft202012Validator.check_schema(report_schema)
        assert list(Draft202012Validator(report_schema).iter_errors(report_json)) == []

    with target_engine.begin() as target:
        target.execute(
            KnowledgeAsset.__table__.insert().values(
                id="asset_other_installation",
                space_id="space_other_installation",
                kind="document",
                title="Unrelated",
                description="",
                mime_type="text/plain",
                source_type="fixture",
                source_uri="knowledge://spaces/space_other_installation/assets/asset_other_installation",
                revision="sha256:other",
                content_digest="sha256:other",
                permissions_json={},
                metadata_json={},
                created_at=moment,
                updated_at=moment,
            )
        )
    with source_engine.connect() as source, target_engine.begin() as target:
        rerun = run_core_catalog_rehearsal(
            source,
            target,
            installation_id="install_1",
            source_revision="legacy-schema-1",
            target_revision="platform-rehearsal-1-rerun",
            active_revision="legacy-active-1",
            file_reference_checker=lambda reference: Path(reference).exists(),
        )
        assert rerun.report.target_tables[1].row_count == 1
    with source_engine.connect() as source:
        assert source.execute(select(legacy_documents.c.id)).scalar_one() == "doc_1"
    source_engine.dispose()
    target_engine.dispose()


def test_connector_catalog_rehearsal_copies_credentials_and_lease_state(tmp_path: Path) -> None:
    from datetime import datetime, timedelta, timezone

    from jsonschema import Draft202012Validator
    from sqlalchemy import JSON, Column, DateTime, Integer, MetaData, String, Table, Text, create_engine, select

    from knowledge_platform.catalog import (
        KnowledgeConnector,
        KnowledgeSourceItem,
        KnowledgeSyncRun,
        RehearsalVerificationError,
        migrate_to_latest,
        run_connector_catalog_rehearsal_with_rollback_probes,
    )
    from knowledge_platform.catalog.connector_rehearsal import _credential_reference, _lease_state_is_valid
    from knowledge_platform.catalog.credential_rehearsal import _safe_ref, _safe_state_hash

    assert _credential_reference("vault://credentials/github-docs") == "vault://credentials/github-docs"
    assert _credential_reference("vault://raw-secret") == "<redacted>"
    assert _credential_reference("vault://users/public/9f5e2d1c") == "<redacted>"
    with pytest.raises(RehearsalVerificationError, match="unsafe"):
        _safe_ref("vault://raw-secret")
    with pytest.raises(RehearsalVerificationError, match="state_hash"):
        _safe_state_hash("")
    assert not _lease_state_is_valid(
        [
            {
                "status": "running",
                "progress": 1,
                "attempt": 1,
                "lease_owner": "worker",
                "lease_expires_at": datetime(2026, 9, 3, 0, 10),
                "heartbeat_at": datetime(2026, 9, 3, 0, 11),
            }
        ],
        as_of=datetime(2026, 9, 3, 0, 5, tzinfo=timezone.utc),
    )

    source_engine = create_engine("sqlite:///:memory:")
    target_engine = create_engine("sqlite:///:memory:")
    source_metadata = MetaData()
    legacy_bases = Table(
        "knowledge_bases",
        source_metadata,
        Column("id", String(64), primary_key=True),
        Column("name", String(200), nullable=False),
        Column("description", Text, nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False),
        Column("updated_at", DateTime(timezone=True), nullable=False),
    )
    legacy_documents = Table(
        "knowledge_documents",
        source_metadata,
        Column("id", String(64), primary_key=True),
        Column("knowledge_base_id", String(64), nullable=False),
        Column("title", String(300), nullable=False),
        Column("source_type", String(40), nullable=False),
        Column("source_path", Text, nullable=False),
        Column("storage_path", Text, nullable=False),
        Column("virtual_path", Text, nullable=False),
        Column("mime_type", String(120), nullable=False),
        Column("content_sha256", String(64), nullable=False),
        Column("status", String(40), nullable=False),
        Column("doc_metadata", JSON, nullable=False),
        Column("origin_url", Text, nullable=True),
        Column("created_at", DateTime(timezone=True), nullable=False),
        Column("updated_at", DateTime(timezone=True), nullable=False),
    )
    legacy_connections = Table(
        "knowledge_source_connections",
        source_metadata,
        Column("id", String(64), primary_key=True),
        Column("knowledge_base_id", String(64), nullable=False),
        Column("connector_key", String(80), nullable=False),
        Column("name", String(200), nullable=False),
        Column("status", String(40), nullable=False),
        Column("auth_type", String(40), nullable=False),
        Column("credential_ref", Text, nullable=False),
        Column("config_json", JSON, nullable=False),
        Column("schedule_json", JSON, nullable=False),
        Column("last_sync_run_id", String(64), nullable=True),
        Column("last_synced_at", DateTime(timezone=True), nullable=True),
        Column("last_error_json", JSON, nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False),
        Column("updated_at", DateTime(timezone=True), nullable=False),
    )
    legacy_items = Table(
        "knowledge_source_items",
        source_metadata,
        Column("id", String(64), primary_key=True),
        Column("knowledge_base_id", String(64), nullable=False),
        Column("source_connection_id", String(64), nullable=False),
        Column("external_id", String(500), nullable=False),
        Column("external_parent_id", String(500), nullable=True),
        Column("external_type", String(80), nullable=False),
        Column("title", String(500), nullable=False),
        Column("source_url", Text, nullable=True),
        Column("path_json", JSON, nullable=False),
        Column("revision", String(200), nullable=True),
        Column("content_sha256", String(64), nullable=True),
        Column("document_id", String(64), nullable=True),
        Column("status", String(40), nullable=False),
        Column("remote_created_at", DateTime(timezone=True), nullable=True),
        Column("remote_updated_at", DateTime(timezone=True), nullable=True),
        Column("last_seen_run_id", String(64), nullable=True),
        Column("metadata_json", JSON, nullable=False),
        Column("permissions_json", JSON, nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False),
        Column("updated_at", DateTime(timezone=True), nullable=False),
    )
    legacy_sync_runs = Table(
        "knowledge_sync_runs",
        source_metadata,
        Column("id", String(64), primary_key=True),
        Column("source_connection_id", String(64), nullable=False),
        Column("mode", String(40), nullable=False),
        Column("status", String(40), nullable=False),
        Column("cursor_json", JSON, nullable=False),
        Column("stats_json", JSON, nullable=False),
        Column("current_step", String(80), nullable=False),
        Column("progress", Integer, nullable=False),
        Column("error_json", JSON, nullable=False),
        Column("started_at", DateTime(timezone=True), nullable=True),
        Column("finished_at", DateTime(timezone=True), nullable=True),
        Column("lease_owner", String(120), nullable=True),
        Column("lease_expires_at", DateTime(timezone=True), nullable=True),
        Column("heartbeat_at", DateTime(timezone=True), nullable=True),
        Column("attempt", Integer, nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False),
        Column("updated_at", DateTime(timezone=True), nullable=False),
    )
    source_metadata.create_all(source_engine)
    source_file = tmp_path / "connector-source.md"
    storage_file = tmp_path / "connector-storage.md"
    source_file.write_text("source", encoding="utf-8")
    storage_file.write_text("source", encoding="utf-8")
    moment = datetime(2026, 9, 3, tzinfo=timezone.utc)
    expires = moment + timedelta(minutes=5)
    with source_engine.begin() as connection:
        connection.execute(
            legacy_bases.insert(),
            {
                "id": "kb_connector",
                "name": "Connectors",
                "description": "Connector fixtures",
                "created_at": moment,
                "updated_at": moment,
            },
        )
        connection.execute(
            legacy_documents.insert(),
            {
                "id": "doc_connector",
                "knowledge_base_id": "kb_connector",
                "title": "Remote policy",
                "source_type": "connector",
                "source_path": str(source_file),
                "storage_path": str(storage_file),
                "virtual_path": "remote/policy.md",
                "mime_type": "text/markdown",
                "content_sha256": "connector-digest",
                "status": "ready",
                "doc_metadata": {},
                "origin_url": None,
                "created_at": moment,
                "updated_at": moment,
            },
        )
        connection.execute(
            legacy_sync_runs.insert(),
            {
                "id": "run_connector",
                "source_connection_id": "conn_connector",
                "mode": "incremental",
                "status": "running",
                "cursor_json": {"token": "cursor-secret"},
                "stats_json": {"seen": 1},
                "current_step": "fetch",
                "progress": 50,
                "error_json": {"api_token": "error-secret"},
                "started_at": moment,
                "finished_at": None,
                "lease_owner": "worker-1",
                "lease_expires_at": expires,
                "heartbeat_at": moment,
                "attempt": 2,
                "created_at": moment,
                "updated_at": moment,
            },
        )
        connection.execute(
            legacy_connections.insert(),
            {
                "id": "conn_connector",
                "knowledge_base_id": "kb_connector",
                "connector_key": "github",
                "name": "GitHub docs",
                "status": "ready",
                "auth_type": "oauth",
                "credential_ref": "vault://credentials/github-docs",
                "config_json": {
                    "api_key": "plain-secret",
                    "repo": "acme/docs",
                    "value": "plain-secret",
                    "endpoint": "https://example.test/path-secret",
                    "ordinary_url": "https://example.test/ordinary",
                    "absolute_path": str(source_file),
                },
                "schedule_json": {"cron": "0 * * * *"},
                "last_sync_run_id": "run_connector",
                "last_synced_at": moment,
                "last_error_json": {"message": ""},
                "created_at": moment,
                "updated_at": moment,
            },
        )
        connection.execute(
            legacy_items.insert(),
            {
                "id": "item_connector",
                "knowledge_base_id": "kb_connector",
                "source_connection_id": "conn_connector",
                "external_id": "remote-1",
                "external_parent_id": None,
                "external_type": "file",
                "title": "Remote policy",
                "source_url": "https://user:password@example.test/policy?token=url-secret",
                "path_json": [
                    {"path": str(source_file), "token": "path-secret"},
                    "remote",
                    "policy.md",
                    "https://user:password@example.test/policy",
                ],
                "revision": "remote-rev-1",
                "content_sha256": "remote-digest",
                "document_id": "doc_connector",
                "status": "ready",
                "remote_created_at": moment,
                "remote_updated_at": moment,
                "last_seen_run_id": "run_connector",
                "metadata_json": {"access_token": "item-secret"},
                "permissions_json": {"scope": "read"},
                "created_at": moment,
                "updated_at": moment,
            },
        )

    with target_engine.begin() as target:
        migrate_to_latest(target)
        target.execute(
            KnowledgeConnector.__table__.insert(),
            {
                "id": "connector_existing",
                "space_id": "space_existing",
                "connector_key": "existing",
                "name": "Existing target connector",
                "status": "ready",
                "auth_type": "builtin",
                "credential_ref": "",
                "config_json": {},
                "schedule_json": {},
                "last_sync_run_id": None,
                "last_synced_at": None,
                "last_error_json": {},
                "created_at": moment,
                "updated_at": moment,
            },
        )

    with source_engine.connect() as source:
        result = run_connector_catalog_rehearsal_with_rollback_probes(
            source,
            target_engine,
            installation_id="install_connector",
            source_revision="legacy-schema-connector-1",
            target_revision="platform-rehearsal-connector-1",
            active_revision="legacy-active-connector-1",
            file_reference_checker=lambda reference: Path(reference).exists(),
            lease_as_of=moment + timedelta(minutes=1),
        )
    assert [snapshot.table for snapshot in result.report.source_tables] == [
        "spaces",
        "assets",
        "datasets",
        "connectors",
        "source_items",
        "sync_runs",
    ]
    assert all(result.report.checks.values())
    with target_engine.connect() as target:
        connector = (
            target.execute(
                select(KnowledgeConnector.__table__).where(
                    KnowledgeConnector.__table__.c.id == "connector_conn_connector"
                )
            )
            .mappings()
            .one()
        )
        item = (
            target.execute(
                select(KnowledgeSourceItem.__table__).where(
                    KnowledgeSourceItem.__table__.c.id == "source_item_item_connector"
                )
            )
            .mappings()
            .one()
        )
        sync_run = (
            target.execute(
                select(KnowledgeSyncRun.__table__).where(KnowledgeSyncRun.__table__.c.id == "sync_run_connector")
            )
            .mappings()
            .one()
        )
        assert connector["credential_ref"] == "vault://credentials/github-docs"
        assert connector["config_json"]["api_key"] == "<redacted>"
        assert connector["config_json"]["value"] == "<redacted>"
        assert connector["config_json"]["endpoint"]["reference_digest"].startswith("sha256:")
        assert connector["config_json"]["ordinary_url"]["reference_digest"].startswith("sha256:")
        assert connector["config_json"]["absolute_path"]["reference_digest"].startswith("sha256:")
        assert item["source_url"] is None
        assert item["path_json"][0]["path"]["reference_digest"].startswith("sha256:")
        assert item["path_json"][0]["token"] == "<redacted>"
        assert str(source_file) not in json.dumps(result.to_dict(), ensure_ascii=False)
        assert "https://user:password@example.test/policy" not in json.dumps(result.to_dict(), ensure_ascii=False)
        assert item["metadata_json"]["source_url_digest"].startswith("sha256:")
        assert item["metadata_json"]["access_token"] == "<redacted>"
        assert sync_run["lease_owner"] == "worker-1"
        assert sync_run["lease_expires_at"] == expires.replace(tzinfo=None)
        report_path = tmp_path / "connector-rehearsal-report.json"
        result.write_json(report_path)
        report_json = json.loads(report_path.read_text(encoding="utf-8"))
        report_schema = json.loads(
            (ROOT.parent / "docs" / "knowledge-platform" / "catalog-rehearsal-report.schema.json").read_text(
                encoding="utf-8"
            )
        )
        assert list(Draft202012Validator(report_schema).iter_errors(report_json)) == []
        assert "plain-secret" not in report_path.read_text(encoding="utf-8")
        assert "url-secret" not in report_path.read_text(encoding="utf-8")
        assert "knowledge_connectors:out_of_scope" in {
            snapshot["table"] for snapshot in report_json["report"]["target_out_of_scope_tables"]
        }
    with source_engine.begin() as connection:
        connection.execute(legacy_sync_runs.update().values(status="paused"))
    invalid_target_engine = create_engine("sqlite:///:memory:")
    with source_engine.connect() as source:
        with pytest.raises(RehearsalVerificationError, match="one or more explicit"):
            run_connector_catalog_rehearsal_with_rollback_probes(
                source,
                invalid_target_engine,
                installation_id="install_connector",
                source_revision="legacy-schema-connector-1",
                target_revision="platform-rehearsal-invalid-lease",
                active_revision="legacy-active-connector-1",
                file_reference_checker=lambda reference: Path(reference).exists(),
                lease_as_of=moment + timedelta(minutes=1),
            )
    with invalid_target_engine.connect() as target:
        assert target.execute(select(KnowledgeConnector.__table__.c.id)).first() is None
    source_engine.dispose()
    target_engine.dispose()
    invalid_target_engine.dispose()


def test_feishu_credential_rehearsal_rebinds_vault_refs_without_secret_payloads(tmp_path: Path) -> None:
    from datetime import datetime, timedelta, timezone

    from jsonschema import Draft202012Validator
    from sqlalchemy import JSON, Column, DateTime, Integer, MetaData, String, Table, Text, create_engine, select

    from knowledge_platform.catalog import (
        KnowledgeCredential,
        KnowledgeCredentialGrant,
        KnowledgeOAuthSession,
        RehearsalVerificationError,
        migrate_to_latest,
        run_feishu_credential_catalog_rehearsal_with_rollback_probes,
    )

    source_engine = create_engine("sqlite:///:memory:")
    target_engine = create_engine("sqlite:///:memory:")
    metadata = MetaData()
    source_connections = Table(
        "knowledge_source_connections",
        metadata,
        Column("id", String(64), primary_key=True),
        Column("config_json", JSON, nullable=False),
    )
    apps = Table(
        "feishu_app_credentials",
        metadata,
        Column("id", String(64), primary_key=True),
        Column("owner_id", String(120), nullable=False),
        Column("app_id_masked", String(120), nullable=False),
        Column("credential_ref", Text, nullable=False),
        Column("api_base_url", String(300), nullable=False),
        Column("app_name", String(200), nullable=False),
        Column("tenant_key", String(200), nullable=False),
        Column("status", String(40), nullable=False),
        Column("validated_at", DateTime(timezone=True), nullable=True),
        Column("rotated_at", DateTime(timezone=True), nullable=True),
        Column("created_at", DateTime(timezone=True), nullable=False),
        Column("updated_at", DateTime(timezone=True), nullable=False),
    )
    grants = Table(
        "feishu_user_grants",
        metadata,
        Column("id", String(64), primary_key=True),
        Column("app_credential_id", String(64), nullable=False),
        Column("source_connection_id", String(64), nullable=True),
        Column("principal_id", String(120), nullable=False),
        Column("open_id", String(200), nullable=False),
        Column("union_id", String(200), nullable=False),
        Column("tenant_key", String(200), nullable=False),
        Column("token_credential_ref", Text, nullable=False),
        Column("granted_scopes", JSON, nullable=False),
        Column("access_expires_at", DateTime(timezone=True), nullable=True),
        Column("refresh_expires_at", DateTime(timezone=True), nullable=True),
        Column("token_version", Integer, nullable=False),
        Column("status", String(40), nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False),
        Column("updated_at", DateTime(timezone=True), nullable=False),
    )
    oauth_sessions = Table(
        "feishu_oauth_sessions",
        metadata,
        Column("id", String(64), primary_key=True),
        Column("state_hash", String(64), nullable=False),
        Column("app_credential_id", String(64), nullable=False),
        Column("source_connection_id", String(64), nullable=False),
        Column("principal_id", String(120), nullable=False),
        Column("redirect_uri", Text, nullable=False),
        Column("verifier_credential_ref", Text, nullable=False),
        Column("requested_scopes", JSON, nullable=False),
        Column("status", String(40), nullable=False),
        Column("expires_at", DateTime(timezone=True), nullable=False),
        Column("consumed_at", DateTime(timezone=True), nullable=True),
        Column("created_at", DateTime(timezone=True), nullable=False),
    )
    metadata.create_all(source_engine)
    moment = datetime(2026, 9, 3, tzinfo=timezone.utc)
    with source_engine.begin() as connection:
        connection.execute(
            source_connections.insert(),
            {"id": "conn_feishu", "config_json": {"app_credential_id": "app_feishu", "tenant_key": "tenant_demo"}},
        )
        connection.execute(
            apps.insert(),
            {
                "id": "app_feishu",
                "owner_id": "principal_owner",
                "app_id_masked": "cli_••••7890",
                "credential_ref": "vault://users/local/credentials/feishu-app-app_feishu",
                "api_base_url": "https://open.feishu.cn",
                "app_name": "Docs app",
                "tenant_key": "tenant_demo",
                "status": "validated",
                "validated_at": moment,
                "rotated_at": None,
                "created_at": moment,
                "updated_at": moment,
            },
        )
        connection.execute(
            grants.insert(),
            {
                "id": "grant_feishu",
                "app_credential_id": "app_feishu",
                "source_connection_id": "conn_feishu",
                "principal_id": "principal_user",
                "open_id": "ou_user",
                "union_id": "on_user",
                "tenant_key": "tenant_demo",
                "token_credential_ref": "vault://users/local/credentials/feishu-user-grant-grant_feishu-v3",
                "granted_scopes": ["wiki:wiki:readonly", "offline_access"],
                "access_expires_at": moment + timedelta(hours=1),
                "refresh_expires_at": moment + timedelta(days=30),
                "token_version": 3,
                "status": "active",
                "created_at": moment,
                "updated_at": moment,
            },
        )
        connection.execute(
            oauth_sessions.insert(),
            {
                "id": "oauth_feishu",
                "state_hash": "oauth-state-secret",
                "app_credential_id": "app_feishu",
                "source_connection_id": "conn_feishu",
                "principal_id": "principal_user",
                "redirect_uri": "https://accounts.feishu.cn/oauth/callback?state=secret-state",
                "verifier_credential_ref": "vault://users/local/credentials/feishu-oauth-verifier-oauth_feishu",
                "requested_scopes": ["wiki:wiki:readonly"],
                "status": "pending",
                "expires_at": moment + timedelta(minutes=10),
                "consumed_at": None,
                "created_at": moment,
            },
        )

    with target_engine.begin() as target:
        migrate_to_latest(target)
        target.execute(
            KnowledgeCredential.__table__.insert(),
            {
                "id": "credential_existing",
                "owner_principal_id": "existing",
                "provider": "feishu",
                "kind": "app",
                "external_key": "existing",
                "display_name": "Existing",
                "api_base_url": "",
                "credential_ref": "",
                "status": "disabled",
                "metadata_json": {},
                "validated_at": None,
                "rotated_at": None,
                "created_at": moment,
                "updated_at": moment,
            },
        )

    with source_engine.connect() as source:
        result = run_feishu_credential_catalog_rehearsal_with_rollback_probes(
            source,
            target_engine,
            installation_id="install_feishu",
            source_revision="legacy-feishu-1",
            target_revision="platform-feishu-1",
            active_revision="legacy-active-feishu-1",
        )
    assert all(result.report.checks.values())
    assert result.report.injected_failure_checkpoints == (
        "after_schema",
        "after_credentials",
        "after_grants",
        "after_oauth_sessions",
        "before_verification",
    )
    with target_engine.connect() as target:
        credential = (
            target.execute(
                select(KnowledgeCredential.__table__).where(
                    KnowledgeCredential.__table__.c.id == "credential_app_feishu"
                )
            )
            .mappings()
            .one()
        )
        grant = (
            target.execute(
                select(KnowledgeCredentialGrant.__table__).where(
                    KnowledgeCredentialGrant.__table__.c.id == "grant_grant_feishu"
                )
            )
            .mappings()
            .one()
        )
        oauth = (
            target.execute(
                select(KnowledgeOAuthSession.__table__).where(
                    KnowledgeOAuthSession.__table__.c.id == "oauth_oauth_feishu"
                )
            )
            .mappings()
            .one()
        )
        assert credential["credential_ref"] == "vault://users/local/credentials/feishu-app-app_feishu"
        assert grant["token_credential_ref"] == "vault://users/local/credentials/feishu-user-grant-grant_feishu-v3"
        assert oauth["verifier_credential_ref"] == "vault://users/local/credentials/feishu-oauth-verifier-oauth_feishu"
        assert oauth["state_hash"].startswith("sha256:")
        assert oauth["redirect_uri_digest"].startswith("sha256:")
        report_text = json.dumps(result.to_dict(), ensure_ascii=False)
        assert "oauth-state-secret" not in report_text
        assert "secret-state" not in report_text
        assert "oauth/callback" not in report_text
        assert "knowledge_credentials:out_of_scope" in {
            snapshot.table for snapshot in result.report.target_out_of_scope_tables
        }
        report_path = tmp_path / "feishu-credential-rehearsal-report.json"
        result.write_json(report_path)
        report_schema = json.loads(
            (ROOT.parent / "docs" / "knowledge-platform" / "catalog-rehearsal-report.schema.json").read_text(
                encoding="utf-8"
            )
        )
        assert list(Draft202012Validator(report_schema).iter_errors(json.loads(report_path.read_text()))) == []
    source_engine.dispose()
    target_engine.dispose()

    invalid_source = create_engine("sqlite:///:memory:")
    invalid_target = create_engine("sqlite:///:memory:")
    metadata.create_all(invalid_source)
    with invalid_source.begin() as connection:
        connection.execute(
            source_connections.insert(),
            {"id": "missing_connection", "config_json": {"app_credential_id": "other", "tenant_key": "tenant"}},
        )
        connection.execute(
            apps.insert(),
            {
                "id": "orphan_app",
                "owner_id": "owner",
                "app_id_masked": "orphan",
                "credential_ref": "vault://users/local/credentials/orphan",
                "api_base_url": "https://open.feishu.cn",
                "app_name": "Orphan",
                "tenant_key": "tenant",
                "status": "pending",
                "validated_at": None,
                "rotated_at": None,
                "created_at": moment,
                "updated_at": moment,
            },
        )
        connection.execute(
            oauth_sessions.insert(),
            {
                "id": "orphan_oauth",
                "state_hash": "b" * 64,
                "app_credential_id": "orphan_app",
                "source_connection_id": "missing_connection",
                "principal_id": "user",
                "redirect_uri": "https://accounts.feishu.cn/oauth/callback",
                "verifier_credential_ref": "vault://users/local/credentials/verifier",
                "requested_scopes": [],
                "status": "pending",
                "expires_at": moment + timedelta(minutes=10),
                "consumed_at": None,
                "created_at": moment,
            },
        )
    with invalid_source.connect() as source:
        with pytest.raises(RehearsalVerificationError, match="binding"):
            run_feishu_credential_catalog_rehearsal_with_rollback_probes(
                source,
                invalid_target,
                installation_id="invalid",
                source_revision="legacy",
                target_revision="platform",
                active_revision="legacy-active",
            )
    invalid_source.dispose()
    invalid_target.dispose()


def test_phase_0a_inventory_is_structured_and_covers_required_domains() -> None:
    inventory_path = ROOT.parent / "docs" / "knowledge-platform" / "phase-0a-inventory.yaml"
    payload = yaml.safe_load(inventory_path.read_text(encoding="utf-8"))
    domains = payload["subdomains"]
    assert payload["format"] == "agent-knowledge-platform-inventory/v1"
    assert len(domains) == 10
    assert {item["target_owner"] for item in domains} >= {"puddingknowledge", "puddingharness", "split"}
    assert all(item["source_paths"] and item["migration"] for item in domains)
    assert len({item["id"] for item in domains}) == len(domains)


def test_credential_register_has_no_literal_secret_values() -> None:
    register_path = ROOT.parent / "docs" / "knowledge-platform" / "config-credential-ownership.yaml"
    payload = yaml.safe_load(register_path.read_text(encoding="utf-8"))
    assert payload["policy"]["secret_values_may_not_cross_boundary"] is True
    assert payload["entries"]
    for entry in payload["entries"]:
        assert entry["target_owner"]
        assert entry["target_secret_backend"]
        assert entry["migration"]
        assert entry["rotation"]


def test_contract_json_schemas_are_language_neutral_drafts() -> None:
    schema_root = ROOT / "knowledge_contracts" / "schemas"
    for name in ("query-result.schema.json", "query-plan.schema.json"):
        payload = json.loads((schema_root / name).read_text(encoding="utf-8"))
        assert payload["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert payload["type"] == "object"
    rehearsal_schema = json.loads(
        (ROOT.parent / "docs" / "knowledge-platform" / "catalog-rehearsal-report.schema.json").read_text(
            encoding="utf-8"
        )
    )
    assert rehearsal_schema["properties"]["report"]["properties"]["active_revision_changed"] == {"const": False}


def test_golden_baseline_registry_is_explicitly_not_claimed_as_frozen() -> None:
    baseline_path = ROOT.parent / "docs" / "knowledge-platform" / "golden-baseline.yaml"
    payload = yaml.safe_load(baseline_path.read_text(encoding="utf-8"))
    assert payload["status"] == "coverage-registry-not-yet-frozen"
    assert len(payload["capabilities"]) >= 9
    for capability in payload["capabilities"]:
        assert capability["test_files"]
        assert capability["command"].startswith("backend/.venv/bin/pytest")
        assert capability["status"] in {"mapped", "executed-legacy-rehearsal"}
    assert "normalized_result_digest" in payload["freeze_requirements"]


def test_file_classification_rules_cover_every_phase_0a_scan_file() -> None:
    classification_path = ROOT.parent / "docs" / "knowledge-platform" / "file-classification.yaml"
    payload = yaml.safe_load(classification_path.read_text(encoding="utf-8"))
    extensions = set(payload["scan_extensions"])
    rules = payload["rules"]
    scanned: set[str] = set()
    for raw_root in payload["scan_roots"]:
        root = ROOT.parent / raw_root
        if root.is_file():
            candidates = [root]
        else:
            candidates = [path for path in root.rglob("*") if path.is_file()]
        scanned.update(
            str(path.relative_to(ROOT.parent))
            for path in candidates
            if path.suffix in extensions and "__pycache__" not in path.parts
        )
    unmatched = [path for path in sorted(scanned) if not any(fnmatch.fnmatch(path, rule["glob"]) for rule in rules)]
    assert not unmatched, f"Phase 0A files must match at least one classification rule: {unmatched}"
    assert all(next(rule for rule in rules if fnmatch.fnmatch(path, rule["glob"]))["target_owner"] for path in scanned)
