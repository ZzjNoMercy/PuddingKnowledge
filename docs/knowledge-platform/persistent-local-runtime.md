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

## Feishu application authentication and Docx sync

The independent runtime accepts `--feishu-config /absolute/feishu.json` together
with `--state-dir`. The same option is forwarded by the Deploy CLI and supervisor.
This configuration explicitly opts the local runtime into the source Admin APIs:

```json
{
  "version": 1,
  "sources": [{
    "id": "feishu_docs",
    "name": "Team Wiki",
    "selection": {"kind": "wiki", "root": "", "wiki_space": "REMOTE_SPACE_ID"},
    "app_id": "cli_APPLICATION_ID",
    "app_secret_env": "KNOWLEDGE_FEISHU_APP_SECRET"
  }]
}
```

On first startup the named environment variable must contain the application
secret. The runtime imports it into its encrypted, owner-scoped Vault; Catalog
contains only a credential reference. Subsequent startup can resolve that
reference without the environment variable. Configuration never contains the
plaintext app secret. The default endpoint is `https://open.feishu.cn`; an
explicit loopback HTTP `endpoint` exists for local protocol fixtures only.
No automatic discovery or sync occurs at startup.

- `POST /v1/sources/feishu_docs:discover` lists the selected remote entries.
- `POST /v1/sources/feishu_docs:sync` with
  `{"idempotency_key":"sync-001","mode":"incremental"}` fetches current Docx
  revisions and publishes immutable raw JSON plus normalized Markdown Assets.
- Use `"mode":"full"` for complete-scan deletion reconciliation. An incremental
  run never deletes entries just because they are absent from that scan.
- The existing Catalog list/read endpoints expose the resulting SourceItems and
  document Assets. The Wiki source provider accepts their explicit revisions.

Both source operations require Admin and explicit Space scope. A sync claim
owns a stable credential/configuration snapshot; each Catalog write verifies
ownership and unchanged configuration. A complete successful replay avoids
remote access. Failure and cancellation preserve a retryable job record, while
OS locks release on process exit. Full-scan failure cannot authorize deletion.
Changing an existing source selection requires an explicit migration, rather
than interpreting a smaller selection as remote deletion. Raw and normalized
objects are written before their Catalog references and bound to the owned
object-store identity.

This is the application-authenticated Docx runtime checkpoint, not complete
Feishu feature parity. Remaining work includes Lark
endpoint support, media/attachment downloads and parser routing, Drive binary
files, complete Bitable schema/relation/live-row APIs, per-item failure isolation,
resumable pagination checkpoints, indexing and Console flows. Bitable entries
currently remain live links; no row values are copied into Catalog. Production
account acceptance, upgrades, rollback and continuity gates are still pending.

## Feishu user OAuth

A source can explicitly select `"auth_type":"user"`. Tenant sources retain
application identity; changing an existing source's auth type requires an
explicit migration. A user source adds these fields to its existing Feishu
configuration:

```json
{
  "auth_type": "user",
  "oauth_redirect_uris": ["http://127.0.0.1:8889/v1/sources/oauth/callback"],
  "oauth_scopes": ["offline_access", "wiki:wiki:readonly", "docx:document:readonly"]
}
```

Register the exact callback URI in the Feishu application configuration. The
URI must also match the configured local service port. The local runtime binds
user flows to its server-owned `knowledge-local` principal; callback bodies
cannot choose a principal. This single-user local binding is not a substitute
for production session authentication.

1. `POST /v1/sources/SOURCE_ID:authorize` with the exact configured
   `{"redirect_uri":"..."}` returns an authorization URL and a 10-minute expiry.
   Open that URL to obtain the user's consent.
2. Feishu's redirect reaches `GET /v1/sources/oauth/callback?state=...&code=...`.
   An explicit client can instead POST exactly `state` and `code` to the same
   path. State is one-shot and bound to the source, principal, application,
   redirect digest, requested scopes and current authorization generation.
3. The existing source sync command now uses the active user grant. The provider's
   actual scopes must cover the configured request; requested scopes never stand
   in for omitted consent evidence. Token expiry and a single invalid-token retry
   invoke serialized, version-fenced refresh. Concurrent rejections of the same
   token reuse the first successful refresh result.
4. `POST /v1/sources/SOURCE_ID:revoke-authorization` revokes local use immediately.
   The response contains `provider_revocation: false`: it does not claim the
   Feishu account's remote consent was revoked. Remote consent must be managed
   through the provider's controls until a documented provider revocation API is
   integrated.

State hashes and grant metadata live in Catalog; application secrets, PKCE
verifiers and access/refresh tokens live only in the Platform Vault. The callback
commits `exchanging` before remote I/O, so cancellation/crash cannot replay a code.
Refresh commits `refreshing` before remote I/O, then writes a fresh encrypted
Vault object and commits its reference with the expected version. An interrupted
or ambiguous refresh requires reauthorization. It never guesses that the old
refresh token remains usable. Superseded encrypted token objects are retained
pending a retention-aware Vault cleanup mechanism; they are not selected by the
active grant. Local revocation increments authorization generation and wins over
in-flight callback/refresh/sync publication.

Tests include real provider HTTP requests and independently started runtime
processes, including SIGKILL during callback and refresh. Production Feishu
consent, production browser authentication, remote revocation and release/upgrade
acceptance remain separate, uncompleted gates.

## Registered Bitable schemas and live pages

The local REST runtime now owns Bitable schema sync and bounded live queries.
Configure a Feishu source with `selection.kind: "bitable"`, `selection.root`
equal to the Base app token, and an explicit initial scope:

```json
"bitable": {
  "tables": [{"table_id": "tblExample", "view_id": "vewExample"}],
  "relations": []
}
```

An omitted or empty table list denies all tables. A blank `view_id` explicitly
selects the whole approved table. The config file seeds the policy only on first
registration; subsequent policy updates belong to the owned Catalog and survive
restart. Editing the initial file does not silently re-expand a narrowed scope.

The existing `POST /v1/sources/SOURCE_ID:sync` reads table and field metadata,
never records. It publishes immutable `table_schema` Assets and linked source
items. Numeric provider field types and exact field names are preserved; field
order alone does not change the schema revision. Full successful sync reconciles
removed scope items. Missing approved tables fail the scan and cannot authorize
remote-deletion inference. Policy removal or view changes immediately invalidate
old linked items, even before the next sync.

The REST operations return the standard QueryResult envelope:

- `GET /v1/bitable/sources`: registered readable Bitable sources and table scope.
- `GET /v1/sources/SOURCE_ID/bitable/policy`: current scope, declared relations,
  and `policy_revision` (Admin + Space).
- `PUT /v1/sources/SOURCE_ID/bitable/policy`: exactly `policy` and
  `expected_revision`. Policy contains `tables` and `relations`; validation uses
  remote visible tables and a serialized final revision check. Concurrent writers
  with the same old revision cannot both succeed. Newly declared relation endpoints
  require already synchronized fields, so first configure tables, sync, then add
  relations. Existing relations whose fields disappear remain visible as stale.
- `POST /v1/sources/SOURCE_ID/bitable/resolve`: exactly `url`; accepts official
  tenant HTTPS Base/Wiki links belonging to this registered app. It returns only
  table metadata, never rows (Admin + Space).
- `GET /v1/sources/SOURCE_ID/bitable/tables/TABLE_ID/schema`: live schema,
  revision, current schema Asset URI and `sync_required` (Query + Space).
- `GET /v1/sources/SOURCE_ID/bitable/relations`: schema-only validation of
  explicitly declared relations. It never proves row uniqueness or performs joins.
- `POST /v1/sources/SOURCE_ID/bitable/query`: exactly `table_id`,
  `schema_revision`, `field_names`, `page_size` and `cursor` (Query + Space).
  Use the synchronized revision, exact field names, page size 1–100 and an empty
  cursor for the first page. Empty field names select all visible schema fields
  up to 100. View scope is server-owned and cannot be overridden in the request.

A relation has `id`, `source_table_id`, `source_field_id`, `target_table_id`,
`target_field_id`, and `cardinality` (`one_to_one`, `one_to_many`, `many_to_one`,
`many_to_many`), with optional `name` and `description`. Reverse duplicates and
self-endpoints are rejected. `schema_valid` is structural validation, not proof
of the declared row cardinality.

Live query reads one provider record page, projects returned fields and checks
current schema before and after the record request. Schema drift requires resync;
local scope/authorization changes discard in-flight results. This observation
cannot provide a provider-side transactional snapshot or guarantee stable rows
across pages. QueryResult evidence references the immutable **schema**, not an
archived copy of live records. Identical schema content may reuse its Asset across
credential rotations; current credentials authorize each live observation.

The encrypted 15-minute cursor is bound to principal, source, app policy, table,
view, schema, fields and page size. It cannot be used to request an arbitrary app.
User OAuth sources additionally require the server-established caller identity to
match the source's authorization principal. The current runtime remains single-user
`knowledge-local`; production session authentication is a separate gate.

Rows are returned to the caller with `Cache-Control: no-store` and are not written
to Catalog, Vault or object storage. This does not control the caller's own storage.
Tests compare all local fixture file bytes before/after a query and check a row
canary across independent process restart. No row cache or full-table download is
implemented. Dedicated Console flows, external MCP exposure, separate preview and
relation CRUD compatibility endpoints, media and production acceptance remain
unfinished; this checkpoint does not declare complete Feishu parity.

## Bitable external MCP and Console

When a Bitable source is configured, `/mcp` now advertises four optional read-only
Tools: `feishu_bitable_list_sources`, `feishu_bitable_describe`,
`feishu_bitable_relations`, and `feishu_bitable_query`. These share the REST read
adapter, authorization and QueryResult contract. Tools accept registered
`source_id` values, never credentials or caller principals. Query arguments are
`source_id`, `table_id`, `schema_revision`, `field_names`, `page_size`, `cursor`;
use an empty cursor for the first page. The descriptor supplies exact JSON Schema
and privacy/structure-evidence semantics. Policy and OAuth mutations are not MCP
Tools. Responses use `Cache-Control: no-store`.

A generic MCP client discovers these capabilities without local business Tool
registration. An independently installed PuddingHarness consumer has been tested
with its temporary Home config pointing to the standalone Platform endpoint;
its dynamically discovered names have the configured server prefix, for example
`platform_feishu_bitable_query`. No Bitable implementation or Feishu credentials
were added to Harness. HTTP protocol tests also cover a Docx-only runtime, which
does not advertise Bitable Tools, and isolation of broken source metadata.

The independent Console now has a Bitable section using the public REST client:
read sources, select a source, synchronize schema, inspect a table, choose exact
fields and page size, query one page and continue with the current cursor. Source,
table, API origin, Space filter, field and page-size changes discard stale results
and pagination. Overlapping or failed requests cannot publish an old response
into the new selection. A table whose schema requires synchronization cannot be
queried through the UI until synchronization succeeds.

The scope editor can remove every table (deny all), add a visible table by ID,
and change its view. The server remains authoritative for visibility and policy
revision checks. An empty view means the whole selected table, not a default
view. Relations are edited as declarations from the policy, separately from
schema validation output. Save and sync invalidate the displayed schema and
rows. Query rows remain only in browser memory; the Console does not persist or
export them. Server responses and errors are rendered through text APIs.

This closes the local Bitable MCP/Console path; it does not establish production
multi-user authentication, provider-side row snapshot isolation, or complete
Feishu/Knowledge parity. Remaining platform work includes media/Drive parsers,
Lark, resumable per-item sync, real Semantic processing, import/export/index,
stateful upgrade/rollback and production continuity. Dedicated legacy preview or
relation CRUD URLs are not implemented; the current Console uses the canonical
single-page query and atomic policy update APIs.

### Owned Feishu media and local PDF parsing

Docx sync now downloads its declared image/file attachments from the authenticated,
fixed Feishu API origin. Individual files are bounded at 8 MiB; one document is
bounded at 128 attachments and 32 MiB combined. No temporary provider URL becomes
a Catalog resource. Attachment names do not determine identity: block and token
identity disambiguate repeated filenames. All bytes are content-addressed in the
Platform state directory before the parent publication transaction commits.

A source may explicitly enable the local MinerU parser:

```json
"parser": {
  "id": "mineru_local",
  "endpoint": "http://127.0.0.1:8000",
  "timeout": 60
}
```

This is a field of the source in the existing `--feishu-config` file. The endpoint
must be an explicit loopback HTTP URL; timeout is bounded at 300 seconds. It is
owned by Knowledge and never read from a Harness/Claw home or provider settings.
The parser uses `/file_parse`, multipart `files`, `return_images=true`, and
`response_format_zip=true`. It accepts a bounded ZIP containing one Markdown file
and declared images, or an explicit top-level JSON Markdown response. Derived
output per document is bounded at 512 files and 64 MiB combined. Unsafe ZIP
paths, links, ambiguous documents, missing images, oversized output, redirects,
and incomplete downloads fail the publication. It never scans or cleans a shared
MinerU output directory.

PDF originals and normalized Markdown have separate Asset identities, MIME types,
bytes, and digests. The original exposes `normalized_markdown` through the existing
Asset derivative list/read endpoints. Parsed images are owned Assets referenced
with stable `knowledge://` URIs. Parent replacement, source deletion, and connector
disablement invalidate previous attachment/derivative reads. A failed download or
parse preserves the previous publication; partially staged objects do not become
readable. Parser configuration changes bypass the Docx revision fast path.

Drive `.md`, `.markdown`, `.txt`, and `.pdf` files are downloaded under the same
8 MiB file bound. Their revision is computed from downloaded bytes. Text must be
valid UTF-8. PDFs without a configured parser retain their original bytes and
explicitly report `parse_status=not_configured`; no normalized derivative is
invented. Incremental Drive checks currently download again to verify content.

This phase supplies one explicit local parser route. Cloud ParserRegistry routing,
remote parse-job checkpoints, Office formats, larger-file support, index activation,
and a production MinerU deployment remain separate work. Tests use a real local
HTTP protocol fixture, not an installed production MinerU service.
