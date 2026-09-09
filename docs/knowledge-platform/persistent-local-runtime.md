# Persistent local Knowledge runtime

The independent runtime can preserve Catalog mutations across process restarts
and compile Wiki from approved local sources through an explicitly configured
HTTP model. It owns the destination Catalog and never writes to source files.

Install the backend using the root README. From the repository root:

```sh
uv run --project backend --no-sync puddingknowledge-local \
  --catalog /absolute/source.sqlite3 --wiki-root /absolute/wiki-seed \
  --state-dir /absolute/knowledge-state \
  --temp-dir /absolute/run-unused --ready-file /absolute/ready.json \
  --port 18989 --wiki-config /absolute/wiki.json
```

The Wiki seed must contain at least one Markdown page and the source Catalog
must contain `space_kb_default` / `dataset_kb_default`. First initialization
copies these inputs into state-dir. Later launches with the same state-dir
reuse its Catalog and copied Wiki pages; the source Catalog and seed are not
consulted again. The direct backend CLI can omit them after initialization.
The Deploy CLI currently still requires both flags when starting its supervisor.
`--structured-config` can be combined with this mode to retain logical dataset
authoring and publication in the same owned Catalog.

Wiki source bindings remain external operator-configured inputs. Published bytes
and completed request replay remain readable after those inputs are removed;
a new compilation still needs the original source bytes and matching Catalog
fingerprint. state-dir does not yet ingest every configured source into an
owned Raw Snapshot store.

A process lock rejects simultaneous owners of the state-dir. Interrupted
initialization leaves `.initializing` and fails closed on restart; inspect that
new incomplete directory before recreating it from the original inputs. Do not
remove the marker from a partially initialized directory to force startup.

Example Wiki configuration:

```json
{
  "version": 1,
  "space_id": "space_kb_default",
  "assets": {"source_a": "/absolute/source.md"},
  "model": {
    "endpoint": "https://provider.example/v1/chat/completions",
    "model": "operator-selected-model",
    "api_key_env": "KNOWLEDGE_MODEL_KEY"
  }
}
```

The endpoint receives a non-streaming chat request and must return one choice
with `finish_reason: "stop"` and a message containing JSON string fields `title`
and `markdown`. The credential is read from the named environment variable.
Remote endpoints require HTTPS; loopback HTTP supports local model servers.
Redirects are refused. Empty, truncated, malformed or tool-call-only output
cannot publish. The source must already match the owned Catalog's Space, URI,
revision and digest. HTTP requests cannot supply physical source paths.

Submit `POST /v1/wiki/assets/source_a:compile` with `snapshot_id`,
`source_revision`, `source_uri`, `content_digest` and `idempotency_key`.
For path-only source bindings, source_revision is the source SHA-256 digest.
A configuration binding may instead specify an object with `path`,
`source_revision` and `content_digest` for an existing Catalog revision.
The returned compilation resource URI identifies a new Wiki Asset. Read it
through `/v1/assets/{output_id}:read` or search `/v1/wiki/query` immediately.

The full source identity is permanently bound to the idempotency key. Publication
bytes, receipt and Catalog Asset commit in one SQLite transaction. Replaying a
completed request after restart returns that publication without another model
call. A failed or interrupted model request may be retried with the same input;
external model work can repeat after a crash, but an incomplete draft is never
published. A source fingerprint identifies an immutable output Asset; a new
source revision produces a new Asset.

Deploy CLI `start --apply` passes `--state-dir` and `--wiki-config` to the
installed runtime. Put persistent state under `<home>/catalog/state` if it should
be included in the existing catalog backup root. Backup consistency during live
writes and full upgrade/rollback remain separate acceptance work.

This is a loopback local service with an explicit local principal. Semantic,
Connector Sync, full import/export/index execution, production
multi-user authentication and complete stateful upgrade/rollback are not yet
included in this runtime composition.

## URL capture and owned sources

Add `--capture-config /absolute/capture.json` while using state-dir:

```json
{"version":1,"allowed_origins":[]}
```

The default fetch policy permits public HTTP(S) only. An operator may explicitly
allow a complete private origin such as `http://127.0.0.1:18080` for a controlled
source. This exception is host configuration; API callers cannot add origins.
Each redirect rechecks DNS and policy, connections use validated IPs, HTTPS
verifies the original hostname, and responses are bounded. A dedicated fetch
process is killed and reaped on cancellation or the 60-second overall timeout.

`POST /v1/captures` accepts `url`, `space_id: "space_kb_default"` and a stable
`idempotency_key`. The response identifies a durable ingestion job and the
published content Asset. `GET /v1/captures` lists captures, and
`POST /v1/captures/jobs/{job_id}:retry` replays a failed or interrupted request.
Completed requests return their original Asset without fetching again. A key
cannot be reused for another canonical URL. URL and retry-key values are kept
in the encrypted local Vault; Catalog metadata contains digests and references.

Raw response bytes and normalized Markdown are immutable content-addressed
objects. Object bytes are fsynced before the Catalog transaction publishes the
Asset references and marks the job complete. An interrupted transaction can
leave an unreferenced object, but cannot expose a partial publication. The
Catalog binds to the object store's persistent identity; restoring only its
SQLite file without the associated objects is rejected. Preserve the entire
owned state directory, including its Vault, when backing up this runtime.

Captured Markdown can be read through the normal Asset read endpoint and
promoted through `POST /v1/captures/assets/{asset_id}:promote`, with
`source_revision` (the capture content digest) and `idempotency_key`. Promotion
requires wiki-config; its `assets` map may be empty because the captured source
is resolved from the owned Catalog and object store. It needs no external source
file and does not fetch the original page again. The same processing/Space scope
checks apply to capture and promotion; query MCP remains read-only.

Article extraction retains article-body selection, noise removal, titles,
WeChat formatting and image links. Image binary caching, reading-status edits,
delete/purge and full Feishu discovery/auth/incremental sync remain outstanding.
The independent Feishu block converter is available as a pure conversion layer;
it does not by itself establish a working Feishu connection or sync runtime.
