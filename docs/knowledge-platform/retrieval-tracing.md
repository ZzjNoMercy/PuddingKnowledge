# Independent retrieval tracing

The local Platform runtime assigns a fresh opaque trace ID to each HTTP request.
REST and MCP document/Wiki query results use this ID; the response also carries
`X-Knowledge-Trace-Id`. Client-supplied correlation headers are not authority and
are not reused. No Claw session, run, goal or graph module is required.

The Platform's request-local recorder implements digest-only `TraceEvent`
production behind the existing TraceSink contract. SQLite persistence belongs to
Platform state at `retrieval-traces.sqlite3`, separate from the Catalog. The file
is created with mode 0600 and is recognized by persistent workspace validation.

Document/Wiki query traces contain:

- A digest of the authenticated principal, tenant, requested Space and capability.
- A digest of the fixed Collection versions, index signatures and generations;
  provider identifier and embedding model identity digest.
- Start/end timestamps for query validation, source verification, embedding,
  Milvus verification/search, BM25, rerank and final publication verification.
- Dense, BM25, RRF and rerank positions, each linked to a stable digest of Asset
  and chunk identity, plus the verified source content revision.
- A final evidence identity/revision/locator digest and the application outcome.

Timestamps include UTC offsets and can be used to measure wall-clock stage
latency. HTTP status is a separate transport fact: HTTP 200 does not turn an
error QueryResult into a successful query. Rank events before final validation
are intermediate results; only a successful final outcome authorizes evidence.
No query body, quote, image bytes, raw source identifier/path, SQL, provider
endpoint or credential enters these events. Digests support comparison, not
reconstruction or anonymization of guessable source values.

The administrator can inspect a trace after the request has completed:

```text
GET /v1/traces/{trace_id}
```

The host's authenticated Principal must have `knowledge.admin` or
`knowledge:admin`, with no tenant binding. A search/read scope alone is refused.
This is local operator diagnostics, not a public multi-tenant trace API. Trace
reads do not recursively produce stored retrieval traces. Retained trace
metadata does not grant access to source content.

Each request permits at most 512 events; each event is at most 16 KiB. The SQLite
store currently permits 100,000 events, retains earlier audit records and refuses
to silently delete them. A request's events are validated and committed in one
transaction. If persistence fails, the host returns HTTP 503 `trace_unavailable`
instead of claiming a successfully audited query. Non-retrieval operations are
not made dependent on this retrieval trace store. An operator must explicitly
manage retention before reaching the limit; automatic pruning is not implemented.
Database, parent and SQLite sidecar symlinks are refused at connection checks;
this is not a claim of immunity to concurrent hostile filesystem replacement.

## Acceptance

From `backend`:

```sh
uv sync --locked --all-extras
PYTHONPATH=. .venv/bin/python -m pytest -q tests/test_knowledge_platform_trace_store.py tests/test_knowledge_platform_retrieval_trace.py tests/test_knowledge_platform_retrieval_trace_runtime.py
```

Enable isolated real Milvus with `KNOWLEDGE_TEST_REAL_MILVUS=1` for the runtime
test. Run Docker acceptance suites sequentially. The model is a deterministic
local HTTP fixture. Coverage includes rank-stage tracing, malformed reranker
outcomes, source revocation, per-request IDs, persistence across restart,
concurrency isolation, admin-only inspection, batch capacity rollback and
rejection of altered/cross-trace records.

This closes retrieval-stage diagnostics only. Database SQL/semantic plan traces,
provider token-usage accounting, a Console trace view, monotonic clock latency,
trace export/retention and production observability remain unfinished. Production
activation remains disabled; the full repository split is not declared complete.
