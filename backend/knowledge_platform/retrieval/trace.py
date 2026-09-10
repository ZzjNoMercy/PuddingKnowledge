"""Request-local digest-only retrieval spans; no host/session dependency."""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from functools import wraps
import hashlib
import json
import uuid

from knowledge_contracts import Correlation, TraceDimension, TraceEvent

_current = ContextVar("knowledge_retrieval_trace", default=None)


def digest(value):
    return "sha256:" + hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


class TraceSession:
    def __init__(self, correlation):
        self.correlation = correlation
        self.events = []

    def event(self, name, phase, *, status="ok", span_id=None, input_digest=None, output_digest=None, dimensions=()):
        if len(self.events) >= 512:
            raise ValueError("Request trace event limit exceeded")
        self.events.append(TraceEvent(trace_id=self.correlation.trace_id, span_id=span_id or uuid.uuid4().hex,
            name=name, phase=phase, timestamp=datetime.now(UTC).isoformat(), correlation=self.correlation,
            status=status, input_digest=input_digest, output_digest=output_digest,
            dimensions=tuple(TraceDimension(k, v) for k, v in dimensions)))


def current_correlation():
    session = _current.get()
    return session.correlation if session is not None else Correlation(uuid.uuid4().hex)


@contextmanager
def request_trace(correlation):
    session = TraceSession(correlation); token = _current.set(session)
    try: yield session
    finally: _current.reset(token)


@contextmanager
def span(name):
    session = _current.get()
    if session is None:
        yield
        return
    sid = uuid.uuid4().hex
    session.event(name, "start", status="running", span_id=sid)
    try:
        yield
    except BaseException:
        session.event(name, "end", status="error", span_id=sid)
        raise
    else:
        session.event(name, "end", span_id=sid)


def fact(name, value, *, status="ok", dimensions=()):
    session = _current.get()
    if session is not None:
        session.event(name, "fact", status=status, output_digest=digest(value), dimensions=dimensions)


def ranks(channel, ranked, rows):
    session = _current.get()
    if session is None: return
    for rank, (ordinal, _score) in enumerate(ranked[:50], 1):
        row = rows[ordinal]
        # Hash stable source/chunk identity; raw IDs, text and metadata never enter traces.
        session.event(channel, "rank", dimensions=(("index", str(rank)),
            ("resource", digest([row["asset_id"], row["chunk_id"]])),
            ("revision", row["content_digest"])))


def traced_query(function):
    @wraps(function)
    async def wrapped(self, **kwargs):
        principal = kwargs["principal"]
        # Authorization outcome is captured without enumerating source assets.
        fact("request_identity", [principal.subject_id, principal.tenant_id, kwargs.get("space_id"), self._capability])
        with span("query_validation"):
            result = await function(self, **kwargs)
        fact("query_result", [[e.asset_id, e.revision, dict(e.locator)] for e in result.evidence],
            status=result.status, dimensions=(("capability", self._capability), ("status", result.status)))
        return result
    return wrapped
