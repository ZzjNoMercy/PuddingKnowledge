"""Bounded append-only SQLite storage for digest-only Platform trace events."""

from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import Any

from knowledge_contracts import Correlation, TraceDimension, TraceEvent


_OPAQUE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
_EVENT_FIELDS = {
    "trace_id", "span_id", "name", "phase", "timestamp", "correlation",
    "status", "input_digest", "output_digest", "dimensions",
}
_MAX_EVENT_BYTES = 16 * 1024


class SqliteTraceSink:
    def __init__(self, database_path: Path, *, max_records: int = 100_000) -> None:
        path = Path(database_path).expanduser().absolute()
        if any(candidate.is_symlink() for candidate in (path, *path.parents)):
            raise ValueError("Trace database path must not contain symlinks")
        if path.exists() and not path.is_file():
            raise ValueError("Trace database path must be a regular file")
        if isinstance(max_records, bool) or not isinstance(max_records, int) or not 1 <= max_records <= 10_000_000:
            raise ValueError("Trace record limit is invalid")
        if not path.parent.is_dir():
            raise FileNotFoundError("Trace database parent directory does not exist")
        if not path.exists():
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            os.close(fd)
        if path.stat().st_mode & 0o077:
            raise ValueError("Trace database permissions are too broad")
        self.database_path = path
        self.max_records = max_records
        with self._connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS trace_events ("
                "sequence INTEGER PRIMARY KEY AUTOINCREMENT, trace_id TEXT NOT NULL, payload TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS trace_events_trace_id_sequence "
                "ON trace_events(trace_id, sequence)"
            )

    def _connect(self) -> sqlite3.Connection:
        self._validate_paths()
        connection = sqlite3.connect(self.database_path, timeout=5.0)
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _validate_paths(self) -> None:
        paths = (self.database_path, *self.database_path.parents)
        if any(candidate.is_symlink() for candidate in paths):
            raise ValueError("Trace database path must not contain symlinks")
        for suffix in ("-journal", "-wal", "-shm"):
            sidecar = Path(str(self.database_path) + suffix)
            if sidecar.is_symlink():
                raise ValueError("Trace database sidecar must not be a symlink")

    async def emit(self, event: TraceEvent) -> None:
        await self.emit_batch([event])

    async def emit_batch(self, events: list[TraceEvent]) -> None:
        if not isinstance(events, list) or not 1 <= len(events) <= 512:
            raise ValueError("Trace event batch size is invalid")
        serialized = [(event, self._serialize(event)) for event in events]
        trace_id = serialized[0][0].trace_id
        if any(item[0].trace_id != trace_id for item in serialized):
            raise ValueError("Trace event batch must use one trace ID")
        await asyncio.to_thread(self._append_batch, trace_id, [item[1] for item in serialized])

    def _append_batch(self, trace_id: str, payloads: list[str]) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            count = connection.execute("SELECT COUNT(*) FROM trace_events").fetchone()[0]
            if count + len(payloads) > self.max_records:
                raise ValueError("Trace record limit reached")
            connection.executemany(
                "INSERT INTO trace_events(trace_id,payload) VALUES (?,?)",
                ((trace_id, payload) for payload in payloads),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def read(self, trace_id: str, limit: int = 256) -> list[dict[str, Any]]:
        if not isinstance(trace_id, str) or not _OPAQUE_ID.fullmatch(trace_id):
            raise ValueError("Trace ID is invalid")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 512:
            raise ValueError("Trace read limit is invalid")
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload FROM trace_events WHERE trace_id=? ORDER BY sequence LIMIT ?",
                (trace_id, limit),
            ).fetchall()
        events: list[dict[str, Any]] = []
        for row in rows:
            try:
                raw = json.loads(row[0])
            except (TypeError, ValueError) as error:
                raise ValueError("Stored trace event is invalid") from error
            try:
                event = self._event_from_raw(raw)
            except (TypeError, ValueError) as error:
                raise ValueError("Stored trace event failed validation") from error
            if event.trace_id != trace_id or event.correlation.trace_id != event.trace_id:
                raise ValueError("Stored trace event trace binding is invalid")
            events.append(json.loads(self._serialize(event)))
        return events

    @staticmethod
    def _serialize(event: TraceEvent) -> str:
        if not isinstance(event, TraceEvent):
            raise ValueError("Trace event has an invalid type")
        try:
            raw = asdict(event)
        except Exception as error:
            raise ValueError("Trace event cannot be serialized") from error
        if set(raw) != _EVENT_FIELDS:
            raise ValueError("Trace event contains unknown fields")
        try:
            validated = SqliteTraceSink._event_from_raw(raw)
            serialized = json.dumps(asdict(validated), ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        except (TypeError, ValueError) as error:
            raise ValueError("Trace event failed validation") from error
        if len(serialized.encode("utf-8")) > _MAX_EVENT_BYTES:
            raise ValueError("Trace event exceeds size limit")
        return serialized

    @staticmethod
    def _event_from_raw(raw: Any) -> TraceEvent:
        if not isinstance(raw, dict) or set(raw) != _EVENT_FIELDS:
            raise ValueError("Trace event contains unknown fields")
        correlation_raw = raw["correlation"]
        if not isinstance(correlation_raw, dict) or set(correlation_raw) != {"trace_id", "request_id"}:
            raise ValueError("Trace correlation has an invalid shape")
        correlation = Correlation(**correlation_raw)
        dimensions_raw = raw["dimensions"]
        if not isinstance(dimensions_raw, (list, tuple)):
            raise ValueError("Trace dimensions have an invalid shape")
        dimensions: list[TraceDimension] = []
        for item in dimensions_raw:
            if not isinstance(item, dict) or set(item) != {"key", "value"}:
                raise ValueError("Trace dimension has an invalid shape")
            dimensions.append(TraceDimension(**item))
        event = TraceEvent(
            trace_id=raw["trace_id"], span_id=raw["span_id"], name=raw["name"],
            phase=raw["phase"], timestamp=raw["timestamp"], correlation=correlation,
            status=raw["status"], input_digest=raw["input_digest"],
            output_digest=raw["output_digest"], dimensions=tuple(dimensions),
        )
        if event.correlation.trace_id != event.trace_id:
            raise ValueError("Trace correlation is not bound to the event")
        return event
