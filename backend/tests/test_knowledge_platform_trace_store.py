import asyncio
import json
import sqlite3
from dataclasses import dataclass

import pytest

from knowledge_contracts import Correlation, TraceDimension, TraceEvent
from knowledge_platform.local.trace_store import SqliteTraceSink


def _event(trace="trace-1", span="span-1", phase="start", value="ok"):
    return TraceEvent(
        trace_id=trace,
        span_id=span,
        name="retrieval",
        phase=phase,
        timestamp="2026-09-10T12:00:00+00:00",
        correlation=Correlation(trace, "request-1"),
        status="ok",
        input_digest="sha256:" + "1" * 64,
        dimensions=(TraceDimension("status", value),),
    )


@pytest.mark.asyncio
async def test_emit_and_read_preserve_append_order(tmp_path):
    store = SqliteTraceSink(tmp_path / "trace.db")
    await store.emit(_event(phase="first"))
    await store.emit(_event(phase="second", span="span-2"))
    events = store.read("trace-1")
    assert [item["phase"] for item in events] == ["first", "second"]
    assert events[0]["correlation"] == {"trace_id": "trace-1", "request_id": "request-1"}
    assert events[0]["dimensions"] == [{"key": "status", "value": "ok"}]


def test_read_limit_and_opaque_trace_id(tmp_path):
    store = SqliteTraceSink(tmp_path / "trace.db")
    with pytest.raises(ValueError):
        store.read("/private/trace")
    with pytest.raises(ValueError):
        store.read("trace", 0)
    with pytest.raises(ValueError):
        store.read("trace", 513)


@pytest.mark.asyncio
async def test_record_limit_rejects_without_deleting_audit_history(tmp_path):
    store = SqliteTraceSink(tmp_path / "trace.db", max_records=1)
    await store.emit(_event())
    with pytest.raises(ValueError, match="limit"):
        await store.emit(_event(span="span-2"))
    assert len(store.read("trace-1")) == 1


@pytest.mark.asyncio
async def test_emit_batch_is_atomic_when_capacity_is_insufficient(tmp_path):
    store = SqliteTraceSink(tmp_path / "trace.db", max_records=2)
    await store.emit(_event(span="existing"))
    with pytest.raises(ValueError, match="limit"):
        await store.emit_batch([_event(span="new-1"), _event(span="new-2")])
    assert [item["span_id"] for item in store.read("trace-1")] == ["existing"]
    with pytest.raises(ValueError, match="one trace"):
        await store.emit_batch([_event(trace="trace-1"), _event(trace="trace-2")])


@pytest.mark.asyncio
async def test_concurrent_batches_do_not_exceed_capacity(tmp_path):
    store = SqliteTraceSink(tmp_path / "trace.db", max_records=20)
    batches = [[_event(span=f"batch-{batch}-{index}") for index in range(5)] for batch in range(8)]
    results = await asyncio.gather(*(store.emit_batch(batch) for batch in batches), return_exceptions=True)
    assert sum(result is None for result in results) == 4
    assert len(store.read("trace-1", 512)) == 20


def test_rejects_symlink_path_and_missing_parent(tmp_path):
    target = tmp_path / "real"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError):
        SqliteTraceSink(link / "trace.db")
    with pytest.raises(FileNotFoundError):
        SqliteTraceSink(tmp_path / "missing" / "trace.db")


@pytest.mark.asyncio
async def test_validation_rejects_unknown_fields_bad_dimensions_and_large_event(tmp_path):
    store = SqliteTraceSink(tmp_path / "trace.db")

    @dataclass(frozen=True)
    class ExtendedTraceEvent(TraceEvent):
        extra: str = "unexpected"

    with pytest.raises(ValueError, match="unknown"):
        base = _event()
        await store.emit(ExtendedTraceEvent(
            base.trace_id, base.span_id, base.name, base.phase, base.timestamp,
            base.correlation, base.status, base.input_digest, base.output_digest, base.dimensions,
        ))
    bad = object.__new__(TraceEvent)
    object.__setattr__(bad, "trace_id", "trace-1")
    object.__setattr__(bad, "span_id", "span-1")
    object.__setattr__(bad, "name", "retrieval")
    object.__setattr__(bad, "phase", "start")
    object.__setattr__(bad, "timestamp", "2026-09-10T12:00:00+00:00")
    object.__setattr__(bad, "correlation", "not-correlation")
    object.__setattr__(bad, "status", "ok")
    object.__setattr__(bad, "input_digest", None)
    object.__setattr__(bad, "output_digest", None)
    object.__setattr__(bad, "dimensions", ())
    with pytest.raises(ValueError):
        await store.emit(bad)
    huge = _event()
    object.__setattr__(huge, "dimensions", (TraceDimension("resource", "x" * 160),) * 120)
    with pytest.raises(ValueError, match="size"):
        await store.emit(huge)


@pytest.mark.asyncio
async def test_concurrent_emits_are_all_persisted(tmp_path):
    store = SqliteTraceSink(tmp_path / "trace.db")
    await asyncio.gather(*(store.emit(_event(span=f"span-{i}")) for i in range(20)))
    assert len(store.read("trace-1", 512)) == 20


@pytest.mark.asyncio
async def test_read_rejects_tampered_or_cross_trace_payload(tmp_path):
    path = tmp_path / "trace.db"
    store = SqliteTraceSink(path)
    await store.emit(_event())
    with sqlite3.connect(path) as connection:
        payload = json.loads(connection.execute("SELECT payload FROM trace_events").fetchone()[0])
        payload["trace_id"] = "other-trace"
        connection.execute("UPDATE trace_events SET payload=?", (json.dumps(payload),))
    with pytest.raises(ValueError):
        store.read("trace-1")

    with sqlite3.connect(path) as connection:
        payload["trace_id"] = "trace-1"
        payload["correlation"]["trace_id"] = "other-trace"
        connection.execute("UPDATE trace_events SET payload=?", (json.dumps(payload),))
    with pytest.raises(ValueError):
        store.read("trace-1")
