"""Versioned migration runner for the independent Harness catalog."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Connection, inspect, select

from .harness_models import HarnessSchemaVersion, HarnessWorkerAccessLog

HARNESS_SCHEMA_VERSIONS = (1,)


def migrate_harness_to_latest(connection: Connection) -> list[int]:
    """Create and validate Harness-owned tables without touching Platform tables."""

    HarnessSchemaVersion.__table__.create(connection, checkfirst=True)
    applied = {int(row[0]) for row in connection.execute(select(HarnessSchemaVersion.version))}
    unknown = sorted(applied - set(HARNESS_SCHEMA_VERSIONS))
    if unknown:
        raise RuntimeError(f"Harness catalog has future schema versions: {unknown}")
    missing_history = set(range(1, max(applied, default=0) + 1)) - applied
    if missing_history:
        raise RuntimeError(f"Harness catalog has non-contiguous schema history: {sorted(missing_history)}")
    if 1 in applied:
        if "worker_access_logs" not in inspect(connection).get_table_names():
            raise RuntimeError("Harness schema version 1 is missing worker_access_logs")
        return []
    HarnessWorkerAccessLog.__table__.create(connection, checkfirst=True)
    connection.execute(
        HarnessSchemaVersion.__table__.insert().values(version=1, applied_at=datetime.now(timezone.utc))
    )
    return [1]
