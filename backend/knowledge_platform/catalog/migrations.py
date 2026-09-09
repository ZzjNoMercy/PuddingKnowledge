"""Versioned migration runner for the independent Platform Catalog."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Connection, inspect, select

from .metadata import KNOWLEDGE_METADATA
from .models import KnowledgeCatalogSchemaVersion

CURRENT_SCHEMA_VERSION = 12
SCHEMA_VERSIONS = (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12)
_V1_TABLES = (
    "knowledge_spaces",
    "knowledge_assets",
    "knowledge_datasets",
    "knowledge_connectors",
    "knowledge_source_items",
    "knowledge_sync_runs",
)
_V2_TABLES = (
    "knowledge_credentials",
    "knowledge_credential_grants",
    "knowledge_oauth_sessions",
)
_V3_TABLES = (
    "knowledge_web_captures",
    "knowledge_ingestion_jobs",
    "knowledge_ingestion_events",
)
_V4_TABLES = ("knowledge_structured_assets",)
_V5_TABLES = ("knowledge_query_results",)
_V6_TABLES = ("knowledge_processing_jobs", "knowledge_processing_events")
_V7_TABLES = ("knowledge_authoring_jobs", "knowledge_authoring_events")
_V8_TABLES = ("knowledge_database_connectors",)
_V9_TABLES = ("knowledge_notification_events",)
_V10_TABLES = ("knowledge_collection_bindings",)
_V11_TABLES = ("knowledge_notification_event_scopes",)
_V12_TABLES = ("knowledge_query_result_scopes",)
_VERSION_TABLES = {
    1: _V1_TABLES,
    2: _V2_TABLES,
    3: _V3_TABLES,
    4: _V4_TABLES,
    5: _V5_TABLES,
    6: _V6_TABLES,
    7: _V7_TABLES,
    8: _V8_TABLES,
    9: _V9_TABLES,
    10: _V10_TABLES,
    11: _V11_TABLES,
    12: _V12_TABLES,
}
_REQUIRED_TABLES_BY_VERSION = {
    1: set(_V1_TABLES),
    2: set(_V1_TABLES + _V2_TABLES),
    3: set(_V1_TABLES + _V2_TABLES + _V3_TABLES),
    4: set(_V1_TABLES + _V2_TABLES + _V3_TABLES + _V4_TABLES),
    5: set(_V1_TABLES + _V2_TABLES + _V3_TABLES + _V4_TABLES + _V5_TABLES),
    6: set(_V1_TABLES + _V2_TABLES + _V3_TABLES + _V4_TABLES + _V5_TABLES + _V6_TABLES),
    7: set(_V1_TABLES + _V2_TABLES + _V3_TABLES + _V4_TABLES + _V5_TABLES + _V6_TABLES + _V7_TABLES),
    8: set(_V1_TABLES + _V2_TABLES + _V3_TABLES + _V4_TABLES + _V5_TABLES + _V6_TABLES + _V7_TABLES + _V8_TABLES),
    9: set(_V1_TABLES + _V2_TABLES + _V3_TABLES + _V4_TABLES + _V5_TABLES + _V6_TABLES + _V7_TABLES + _V8_TABLES + _V9_TABLES),
    10: set(_V1_TABLES + _V2_TABLES + _V3_TABLES + _V4_TABLES + _V5_TABLES + _V6_TABLES + _V7_TABLES + _V8_TABLES + _V9_TABLES + _V10_TABLES),
    11: set(_V1_TABLES + _V2_TABLES + _V3_TABLES + _V4_TABLES + _V5_TABLES + _V6_TABLES + _V7_TABLES + _V8_TABLES + _V9_TABLES + _V10_TABLES + _V11_TABLES),
    12: set(_V1_TABLES + _V2_TABLES + _V3_TABLES + _V4_TABLES + _V5_TABLES + _V6_TABLES + _V7_TABLES + _V8_TABLES + _V9_TABLES + _V10_TABLES + _V11_TABLES + _V12_TABLES),
}


def _schema_signature(connection: Connection, table_name: str) -> dict[str, object]:
    """Return the dialect-observed schema shape for one Catalog table."""

    inspector = inspect(connection)
    return {
        "columns": tuple(
            (str(column["name"]), str(column["type"]), bool(column["nullable"]))
            for column in inspector.get_columns(table_name)
        ),
        "primary_key": tuple(inspector.get_pk_constraint(table_name).get("constrained_columns") or ()),
        "indexes": tuple(
            sorted(
                (
                    str(index.get("name") or ""),
                    bool(index.get("unique")),
                    tuple(str(column) for column in index.get("column_names") or ()),
                )
                for index in inspector.get_indexes(table_name)
            )
        ),
        "unique_constraints": tuple(
            sorted(
                (
                    str(constraint.get("name") or ""),
                    tuple(str(column) for column in constraint.get("column_names") or ()),
                )
                for constraint in inspector.get_unique_constraints(table_name)
            )
        ),
        "foreign_keys": tuple(
            sorted(
                (
                    tuple(str(column) for column in foreign_key.get("constrained_columns") or ()),
                    str(foreign_key.get("referred_table") or ""),
                    tuple(str(column) for column in foreign_key.get("referred_columns") or ()),
                )
                for foreign_key in inspector.get_foreign_keys(table_name)
            )
        ),
    }


def _expected_schema_signature(table: object) -> dict[str, object]:
    # The object is a SQLAlchemy Table; keeping this helper untyped avoids
    # importing SQLAlchemy's large Table typing surface into the migration API.
    return {
        "columns": tuple(
            (str(column.name), str(column.type), bool(column.nullable))
            for column in table.columns  # type: ignore[attr-defined]
        ),
        "primary_key": tuple(column.name for column in table.primary_key.columns),  # type: ignore[attr-defined]
        "indexes": tuple(
            sorted(
                (
                    str(index.name or ""),
                    bool(index.unique),
                    tuple(column.name for column in index.columns),
                )
                for index in table.indexes  # type: ignore[attr-defined]
            )
        ),
        "unique_constraints": tuple(
            sorted(
                (
                    str(constraint.name or ""),
                    tuple(column.name for column in constraint.columns),
                )
                for constraint in table.constraints  # type: ignore[attr-defined]
                if constraint.__class__.__name__ == "UniqueConstraint"
            )
        ),
        "foreign_keys": tuple(
            sorted(
                (
                    (str(foreign_key.parent.name),),
                    str(foreign_key.column.table.name),
                    (str(foreign_key.column.name),),
                )
                for foreign_key in table.foreign_keys  # type: ignore[attr-defined]
            )
        ),
    }


def _validate_existing_schema(connection: Connection, versions: set[int]) -> None:
    """Fail closed if an applied Catalog version has a drifted table shape."""

    existing_tables = set(inspect(connection).get_table_names())
    history_name = KnowledgeCatalogSchemaVersion.__table__.name
    if history_name in existing_tables:
        expected_history = _expected_schema_signature(KnowledgeCatalogSchemaVersion.__table__)
        actual_history = _schema_signature(connection, history_name)
        if actual_history != expected_history:
            raise RuntimeError(
                f"Platform Catalog schema drift for {history_name}: "
                f"expected={expected_history}, actual={actual_history}"
            )
    required_tables = set().union(*(set(_REQUIRED_TABLES_BY_VERSION[version]) for version in versions))
    for table_name in sorted(required_tables):
        if table_name not in existing_tables:
            continue
        expected = _expected_schema_signature(KNOWLEDGE_METADATA.tables[table_name])
        actual = _schema_signature(connection, table_name)
        if actual != expected:
            raise RuntimeError(f"Platform Catalog schema drift for {table_name}: expected={expected}, actual={actual}")


def migrate_to_latest(connection: Connection) -> list[int]:
    """Create/upgrade Platform Catalog tables on an explicit connection.

    The caller owns the transaction. This function never imports the legacy
    Base and never creates Harness tables.
    """

    KnowledgeCatalogSchemaVersion.__table__.create(connection, checkfirst=True)
    history_signature = _schema_signature(connection, KnowledgeCatalogSchemaVersion.__table__.name)
    expected_history_signature = _expected_schema_signature(KnowledgeCatalogSchemaVersion.__table__)
    if history_signature != expected_history_signature:
        raise RuntimeError(
            f"Platform Catalog schema drift for {KnowledgeCatalogSchemaVersion.__table__.name}: "
            f"expected={expected_history_signature}, actual={history_signature}"
        )
    applied = {int(row[0]) for row in connection.execute(select(KnowledgeCatalogSchemaVersion.version)).all()}
    unknown = sorted(applied - set(SCHEMA_VERSIONS))
    if unknown:
        raise RuntimeError(f"unsupported future Platform Catalog schema versions: {unknown}")
    if applied:
        expected_history_prefix = set(range(1, max(applied) + 1))
        if applied != expected_history_prefix:
            raise RuntimeError(f"non-contiguous Platform Catalog schema history: {sorted(applied)}")
    existing_tables = set(inspect(connection).get_table_names())
    if not applied:
        catalog_tables = set().union(*(set(tables) for tables in _VERSION_TABLES.values()))
        preexisting_without_history = sorted(catalog_tables & existing_tables)
        if preexisting_without_history:
            raise RuntimeError(f"Platform Catalog tables exist without schema history: {preexisting_without_history}")
    missing_applied_tables = {
        version: sorted(_REQUIRED_TABLES_BY_VERSION[version] - existing_tables)
        for version in sorted(applied)
        if _REQUIRED_TABLES_BY_VERSION[version] - existing_tables
    }
    if missing_applied_tables:
        raise RuntimeError(f"Platform Catalog schema history has missing tables: {missing_applied_tables}")
    # Validate every physically present Platform table, including tables for
    # pending versions. ``checkfirst=True`` must never turn a pre-created,
    # malformed future table into an apparently successful migration.
    _validate_existing_schema(connection, set(SCHEMA_VERSIONS))
    pending = [version for version in SCHEMA_VERSIONS if version not in applied]
    for version in pending:
        table_names = _VERSION_TABLES[version]
        KNOWLEDGE_METADATA.create_all(
            connection,
            tables=[KNOWLEDGE_METADATA.tables[name] for name in table_names],
        )
        connection.execute(
            KnowledgeCatalogSchemaVersion.__table__.insert().values(
                version=version,
                applied_at=datetime.now(timezone.utc),
            )
        )
    return pending
