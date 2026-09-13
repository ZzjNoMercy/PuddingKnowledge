# Legacy Wiki archive boundary

The old Wiki brain root contains more than published Markdown. Raw snapshots,
their JSONL manifest, schema packs, index and log, publication receipts and
retired pages are part of the evidence needed to migrate it. The existing local
workspace page copier is a query projection and does not preserve this evidence.

`knowledge_platform.distribution.wiki_archive` preserves an explicitly selected
offline brain root, including unknown regular files and empty directories. It
checks every declared Raw snapshot against its bytes, bounds the inventory and
refuses links and special files. The output is private and inactive. The source
must already be a consistent offline snapshot; this command does not stop old
writers or prove an installed source version.

```sh
python -m knowledge_platform.distribution.wiki_archive \
  --source-root /absolute/offline/brain \
  --output /absolute/new/wiki-candidate \
  --installation-id installation-1 --source-revision legacy-1
python -m knowledge_platform.distribution.wiki_archive \
  --verify --output /absolute/new/wiki-candidate
```

Repeat the same preparation command to resume an interrupted copy. Changed
commitments, unknown output files and completed payload tampering are errors.
Verification uses the candidate alone and does not require the original root.
Keep the candidate private: it contains all source files, potentially including
local configuration and historical metadata.

Archive integrity does not establish schema compatibility, page frontmatter or
link correctness, receipt lineage, successful compilation, or query acceptance.
Schema files and `AGENTS.md` are preserved as data and are never executed by the
archiver. `wiki_semantics_verified`, `complete_installation_migration` and
`activation_allowed` remain false. The aggregate protocol and owned query workspace integrations are described below.

The next migration step must build an owned Catalog/query projection from this
verified evidence, preserve original identities and Raw consumption lineage, and
prove independent restart and compile/publish behavior before changing those
claims. Installation cutover and rollback remain separate requirements.

## Aggregate migration request v2

The existing `migrate_from_claw` process entry accepts an explicit
`puddingknowledge-migrate-from-claw-request/v2` request. It has every v1 field
plus required `source_wiki_root`, an absolute directory inside the independently
approved `--source-snapshot`. Requests without Wiki retain the exact v1 schema.

The unchanged receipt v1 includes the archive plan, checkpoint, manifest and
all payload files in its digest-bound `artifacts`. `covered_domains` adds
`wiki_archive`; `pending_domains` retains `wiki`. This distinction allows the
existing Harness verifier to admit the evidence without importing Knowledge
code or claiming that Wiki compilation/query migration is complete.

The aggregate receipt is bounded by the existing Harness contract: 1 MiB JSON,
10,000 artifacts, 64 MiB per file and 256 MiB total. These limits are narrower
than the standalone archiver; exceeding them fails before publishing a receipt.
Resume retains the original request commitment, rejects v2-to-v1 downgrade, and
verifies completed artifacts before any domain operation. Source archives and
the copied Wiki are rechecked before the final receipt. Existing installation
activation flags remain false.

The owned workspace can consume this evidence as a query projection, described
below. Wiki semantic compatibility and combined document/Wiki workspace merge
remain pending.

## Owned Wiki query workspace

A new local workspace can consume a verified archive directly:

```sh
python -m knowledge_platform.local \
  --wiki-archive /absolute/migration/wiki \
  --state-dir /absolute/knowledge/wiki-state \
  --temp-dir /absolute/new/unused-temp \
  --ready-file /absolute/new/wiki-ready.json --port 18882
```

Restart with the same `--state-dir` and new ready/temp paths, omitting
`--wiki-archive`. The workspace owns `wiki-evidence`, preserving the complete
archive envelope, and a separate Catalog containing its active Wiki pages.
The original brain root and migration archive can be disconnected after
initialization. Their deletion is never performed by the runtime.

Space and Collection identity are derived from the explicit installation
identity; page identities additionally bind the relative Wiki slug. Page content
changes and archive relocation do not create new identities. Read and query
bindings use owned files, while the original Raw/schema/history remain evidence.

Initialization is for a new workspace only. It cannot yet merge with an existing
document workspace. A partial initialization retains its marker and fails closed.
This mode provides a query projection; schema/compiler compatibility and complete
installation migration remain unverified and activation remains disabled.

## Documents and Wiki in one new workspace

Provide both verified inputs to initialize one Catalog and one local service:

```sh
python -m knowledge_platform.local \
  --document-migration /absolute/migration/candidate \
  --wiki-archive /absolute/migration/wiki \
  --state-dir /absolute/knowledge/combined-state \
  --temp-dir /absolute/new/unused-temp \
  --ready-file /absolute/new/combined-ready.json --port 18882
```

The combined workspace retains both domain manifests, document blobs and the
complete Wiki archive. It merges only Wiki Spaces, Assets and Collections into
the owned document Catalog, rejecting identities that collide and incompatible
schemas or unsupported nonempty Wiki tables. It never overwrites existing rows.
On restart both domains validate against the same Catalog and their owned files;
input migration directories are no longer required. Queries retain their own
Space and capability boundaries.

This initializes a new workspace; it does not merge into an existing active
workspace. An interrupted initialization remains explicitly incomplete. This
step does not prove cross-store source consistency, compiler/schema compatibility,
writer cutover or complete installation rollback.

## Compile and publish in the owned Space

Wiki service configuration v2 retains the v1 JSON fields (`version`, `space_id`,
`assets`, `model`) and accepts any valid Space that exists in the target Catalog.
Bind `assets` explicitly to owned workspace files; credentials remain environment
references in the model configuration. V1 retains its default-Space restriction.

With `--wiki-config`, v2 publishes the new Asset, its durable compilation receipt
and membership in a dedicated compiled-Wiki Collection in one SQLite transaction.
Existing Collection ownership mismatches reject the publication. Migration-page
validation continues to protect the original archive projection while allowing
new compiled Assets in the same Space. Processing directories are private.

The dedicated Collection can be selected with `collection_id` in `/v1/query`.
Routed Wiki results are restricted to that Collection's Asset IDs. Retrieval uses
bounded provider candidates; it does not promise exhaustive matching over every
page. Same-key replay after restart reuses the committed publication.

This proves independent target compilation mechanics, not compatibility with
legacy schema packs, old compilation receipts or every Raw consumption/retirement
rule. The installed acceptance uses a local deterministic HTTP model fixture;
it does not establish external model quality or production activation.


## Owned Raw Assets

New Wiki workspace manifests use version 5. Each registered immutable Raw snapshot is also a `raw_snapshot` Catalog Asset, with an ID derived from Space and snapshot path and a revision bound to its bytes. The complete original manifest remains in owned archive evidence; Catalog metadata exposes portable source/asset identities, snapshot path, bundle hash and creation time, without publishing the original host source path. Registered Raw files can be read through the Asset API and selected in Wiki config v2 for compilation after migration inputs are disconnected. The current compiler accepts UTF-8 sources up to 8 MiB; archival/read preservation does not imply every binary or larger source can compile.

Raw bindings are separate from published Wiki and document retrieval bindings. Only published page Assets enter the migrated Wiki Collection; Raw never becomes a Wiki search result merely because it contains Markdown. A workspace containing Raw and an empty Wiki directory can initialize before its first compilation. Combined workspaces retain a version 4 outer manifest and a version 5 nested Wiki manifest. Existing version 3 Wiki manifests remain readable without automatic mutation or Raw import.

Restart rederives Raw facts and exact bindings from verified archive bytes and rejects mismatching Catalog/manifest facts. Original receipt lineage, schema validation, consumed/pending status and retirement compatibility remain separate incomplete migration work: presence of a Raw Asset does not classify it as unprocessed or authorize automatic recompilation.


## Historical compilation and retirement lineage

New Wiki workspace manifests use version 6. The Raw metadata projection replays supported published Legacy receipts in chronological order, using job ID as the tie breaker. Only `consumed_raw_by_page` with a matching registered Raw hash confers `historical_consumed`; a Raw mentioned only in `raw_hashes` remains without recorded consumption. A retirement removes its page from the current historical page list while preserving Raw consumption and publishing job IDs. Applicable retirement events retain the old slug, replacement slug, job ID, time and receipt digest. A later publish can legitimately reuse a retired slug. Workspace prefix-migration receipts update page associations.

The Asset list/search `wiki_lineage` field exposes `historical_consumed`, `historical_compiled_pages`, `historical_job_ids`, `historical_compiled_at`, `historical_receipt_digests` and `historical_retirements`. All values are rederived from verified owned archive evidence on startup. Malformed published receipts, mismatched consumed hashes, missing retirement archives and a retired page still active without a later publish reject version 6 initialization. Existing version 3/5 workspaces remain readable with their original projection semantics.

These fields report historical receipt claims, not new compiler success or independent validation of old lint/schema results. Absence of recorded consumption does not itself authorize scheduling or prove a Raw is pending. Raw bytes, original schema and complete original receipts remain preserved. Full legacy schema/frontmatter validation and a reconciled scheduler combining historical and new compilation events are still required.


## Reconciled processing evidence

`GET /v1/wiki/assets/{asset_id}/processing` reads a Raw Asset's historical lineage and current local compilation publications in one SQLite read transaction. It requires read (or admin) plus the target Space scope. A successful current-revision claim must match its request fingerprint, canonical output identity, committed nonempty Markdown digest and published Catalog Asset. Merely setting a row to succeeded cannot establish coverage. Results contain portable output URIs/digests and exclude model content, receipt identifiers and host paths.

`coverage` distinguishes `current_revision_compiled`, `historical_consumption_recorded` and `no_consumption_record`. `unsettled_attempts` counts retained current-revision incomplete attempts, including failed attempts whose durable rows remain retryable. It does not claim that a Worker is live: `live_execution_known=false`. Older-revision attempts are validated before exclusion from current coverage; malformed old rows are not silently filtered away. This read-only reconciliation does not schedule work: `automatic_scheduling_allowed=false`; absence of evidence is not a pending-work decision.


## Independent deterministic lint rules

`knowledge_platform.wiki.lint.lint_workspace` is the internal application port for the legacy Wiki page rules. Its caller supplies a resolved `WikiLintContract`, immutable page/index text, log-presence evidence, verified Raw digest observations and the Raw manifest digest. It checks required frontmatter, page types and paths, schema version, sources, Wiki links, source/media reciprocal relations, index coverage, log presence and Raw integrity observations. Root index/log files remain distinct from nested pages of the same name.

Inputs are bounded to 5000 pages, 8 MiB per text, 64 MiB total text, 64 KiB frontmatter and 10000 findings. YAML aliases, duplicate mapping keys and unsafe tags are rejected. PyYAML is a locked runtime dependency, so this port works in a no-dev independent installation. Ordinary supported YAML/rule results are compared with a frozen extraction of the legacy deterministic lint implementation; malformed YAML handling is deliberately stricter.

This port is not schema bundle admission and is not yet connected to the model compiler's publication validator. The caller-supplied bundle hash is provenance, not authority. Complete migration still needs the actual parent/borrowed pack closure, legacy inheritance resolution, custom/brain/resolved/AGENTS hash checks and a trusted compiler binding. The current raw Home archive may omit parent packs stored in installation resources; it cannot infer them from brain.schema.yaml. No archived AGENTS instructions or schema-specified executable hooks are executed.

## Pure schema bundle admission

`knowledge_platform.wiki.schema.admit_schema_bundle` verifies explicit custom YAML, brain YAML, generated AGENTS Markdown and the exact catalog used by the legacy resolver. It requires two external commitments: the legacy bundle hash and a SHA-256 commitment over all input text hashes, including every catalog entry. A borrowed pack's unused-byte change can leave the old bundle hash unchanged; the new closure commitment detects it. Callers must obtain expected commitments from owned installation/migration evidence, never model-provided claims.

Resolution retains legacy declaration ordering, custom overrides, extends depth/cycle validation and direct explicit borrow selection. It deliberately does not recursively expand a borrowed target's own parents or borrowing, because the legacy resolver did not do so. The catalog is the exact set of ancestors plus direct custom borrow targets used by that algorithm, not a general gbrain dependency closure. Missing packs, unknown borrow selections, duplicate/alias/unsafe YAML, malformed declarations and generated AGENTS mismatch fail closed. Schema hooks, paths and AGENTS are data; no file access, subprocess, regex hook execution or model calls occur.

A frozen fixture generated from the original pure rules verifies exact resolved YAML and old bundle hash. Stricter rejection of ambiguous YAML, unsafe names/prefixes, unknown contract types and unused catalog entries is intentional. This pure admission is not yet wired to archive capture, installed schema ownership or compiler publication. It proves input consistency against supplied commitments, not the authority supplying those commitments. Full migration and activation remain incomplete.

## Captured schema evidence and owned workspaces

The offline capture CLI binds actual external pack bytes to a verified Wiki archive:

```sh
python -m knowledge_platform.distribution.wiki_schema_evidence \
  --wiki-archive /absolute/archive \
  --pack gbrain-base-v2=/absolute/installation/resources/gbrain-base-v2.yaml \
  --expected-bundle-hash HASH_FROM_SOURCE_BUNDLE \
  --output /absolute/private-output/schema.json
```

Provide one `--pack NAME=ABSOLUTE_PATH` for each actual ancestor/direct borrow target. No default catalog is inferred. The output parent must be private and owned. Identical completed output is replayable; changed commitments reject. Capture compares external resource bytes before and after admission and verifies the archive under its shared gate. This is an offline operation, not a live cross-installation writer fence or authentication of arbitrary operator-selected files.

On first persistent startup, combine `--wiki-archive` with `--wiki-schema-evidence /absolute/private-output/schema.json`. This also works alongside `--document-migration`. Wiki workspace version 7 owns a private `wiki-schema.json`, binds its digest in the workspace manifest, and re-admits it against owned archive bytes on every load. Combined version 4 accepts nested version 7. Restart needs only `--state-dir`; original archive and installation resources may be removed. Existing versions 3/5/6 continue to read unchanged. Loading retains the archive shared gate across validation and binding reads and performs a final inventory verification.

The returned `schema_bundle` binds the schema-aware compiler publication validator described below. Evidence and workspace manifests are local integrity commitments, not signatures against an attacker who can rewrite all owned files and hashes. The owning installation's identity/writer authority, full schema update lifecycle and activation/rollback remain separate unfinished work.

## Schema-gated single-page compilation

When the local runtime opens a schema-bearing migrated workspace and enables Wiki compilation, it binds the admitted schema to that Space's compiler. The model receives the selected exact Raw path, schema fields/prefixes and existing slugs, and returns `path`, `title`, `markdown` with YAML frontmatter. It cannot supply a schema commitment or validation receipt. Only owned, registered UTF-8 Raw revisions are accepted; the validator independently hashes the source text and binds title, path, body, source and schema commitments in its receipt.

The publisher recomputes that receipt and lints the complete archive-plus-published workspace within its SQLite `BEGIN IMMEDIATE` transaction. Schema page projection, append-only publication log, Catalog Asset/Collection and compilation success commit together. Index coverage is derived deterministically from the preserved archived index and committed new slugs. Broken links, invalid types/frontmatter, wrong Raw attribution or a later publication failure roll back the whole operation. Existing publication bytes, Asset digest, receipt and source-log records are checked before model context and idempotent replay; success labels alone are insufficient.

This uses the current one-page draft API. It rejects overwriting archived or conflicting published slugs and does not implement multi-page patches, updates, retirement, or schema upgrades. A migrated baseline that fails its schema lint is rejected rather than silently repaired. A Space with preexisting schema-free compilation Assets cannot silently promote them to schema-validated status. Other legacy workspaces retain their previous compiler behavior. Transactional multi-page authoring and lifecycle parity remain required before complete separation/activation.

## Transactional multi-page authoring port

`WikiPatch` carries the exact workspace revision, a bounded set of creates/updates/retirements with per-page digest preconditions, selected Raw paths, final index and one appended log entry. The planner evaluates links and schema against the whole final page set, so mutually linked pages can be created together and retirement must remove/rewrite incoming links. A replacement must survive in the final set; its declared relationship is recorded in the commit ledger, not inferred as a fact from page text.

`WikiAuthoringStore` applies the plan under `BEGIN IMMEDIATE`, commits pages/index/log/history together and stores before-images for updates and retirement bytes/replacement metadata. Exact operation retries return the committed revision; different requests with the same operation ID reject. Workspace revision commits schema/closure, page bytes, index/log and Raw facts. This is local integrity checking, not a signature against an owner rewriting every commitment. `_after_page` is only a crash-test injection hook, never an external event/callback API.

`from_owned_workspace` seeds this port from verified owned schema/Raw and current validated single-page publications. Capturing that snapshot and creating the authoring ownership row share one transaction. Once explicitly opened, the prior single-page writer rejects new work and in-flight publication for that Space, preventing two independent writers. Initialization lints the baseline before storing it. Concurrent stale patches and mid-write process death leave no partial mutation.

The port is not automatically enabled at startup. Active-page Catalog/query/read projection and explicit HTTP administration are described below. Multi-page model orchestration, Console integration and lifecycle recovery commands remain unfinished. No production activation or complete repository separation claim follows from these transaction tests.

## Current authoring Catalog and read/query projection

Owned authoring now commits its active Catalog Assets and a `Current Wiki` Collection in the same SQLite transaction as page/index/log/history changes. Active Asset IDs are stable per Space and slug; page updates retain their URI and replace the content digest. Retirement removes the active Asset and collection membership, while before-images and retirement history remain in the authoring ledger. Immutable migrated archive and previous single-page compilation Assets retain their original URIs and collections as historical evidence. Select the Current Wiki collection when querying current state; a broad Space query can also include historical collections.

`from_owned_workspace` explicitly upgrades the earlier unprojected authoring state by adding and setting the persisted projection flag and creating its Catalog projection under the same transaction. It refuses existing conflicting projection ownership. Every authoring read checks the active Assets and collection against the committed authoring state; disabling an already-bound projection, deleting Assets, or forging digests/collection membership rejects. A projection failure rolls back the patch as well. Query candidate bytes and Catalog facts come from one SQLite read transaction; a WAL concurrent-update test verifies the snapshot boundary.

On independent local server restart, an already-existing authoring state is re-opened and supplies dynamic Wiki read/query services even without a model configuration. This does not create or enable authoring automatically. Multi-page model orchestration and lifecycle recovery commands remain open; historical collections are deliberately retained rather than relabelled as current. Production activation and complete repository separation remain unproven.

## Explicit multi-page authoring administration

Start the independent local server with `--state-dir /absolute/owned-home --wiki-authoring`. The Home must contain admitted Wiki schema evidence. This explicitly establishes the patch writer and fences the previous single-page writer. Without the flag, existing authoring results remain readable but administration routes are absent. This enables local editing, not installation cutover.

The JSON-only routes below require `knowledge.admin` and the exact `knowledge.space:<space_id>` scope together; tenant-scoped requests reject. The local CLI grants administration for its explicit configurations. HTTP request streams are limited to 32 MiB; duplicate keys, non-finite constants, excessive nesting and unknown fields reject. Responses omit physical paths and internal exception text.

- `POST /v1/wiki/authoring/context`: `{ "space_id": "SPACE", "slugs": [], "selected_raw": ["source.md"] }`. Returns revision, schema commitments/contract, index, log digest, page inventory/digests and explicitly selected page/Raw text. Raw selection uses exact registered snapshot paths and reverified owned archive bytes. Select at most 100 pages and 100 Raw snapshots, with separate 16 MiB aggregate text budgets and 8 MiB per Raw. Input paths never grant filesystem authority.
- `POST /v1/wiki/authoring/preview`: `{ "space_id": "SPACE", "patch": PATCH }`. Validates the whole final workspace without mutation; returns proposed revision, request digest and changed/retired slugs.
- `POST /v1/wiki/authoring/apply`: `{ "space_id": "SPACE", "operation_id": "OPERATION", "patch": PATCH }`. Revalidates and atomically commits. Retry the exact request and operation ID after an uncertain response; altered requests using that ID reject. Preview does not reserve the revision.

`PATCH` has `expected_revision`, `changes`, `selected_raw`, `index` and `log_entry`. Each change has `slug`, `markdown`, `expected_digest`, and optional `replacement`. Create uses a null expected digest; update/retirement requires the exact current digest; null Markdown retires the page. The index describes the entire final workspace. Every written page must cite selected immutable Raw. Example shape:

```json
{"expected_revision":"HASH_FROM_CONTEXT","changes":[{"slug":"concepts/example","markdown":"SCHEMA_VALID_MARKDOWN","expected_digest":null}],"selected_raw":["source.md"],"index":"[[concepts/example]]","log_entry":"Created a source-supported concept."}
```

Clients can author multi-page patches through HTTP without importing the internal store. Model generation is described below; scheduling, Console editing, recovery commands and whole-installation cutover/rollback still require implementation and validation.

## Durable model-generated multi-page proposals

Add `--wiki-authoring-model-config /absolute/model.json` alongside `--wiki-authoring` to enable generation. The explicit config has `endpoint`, `model` and optional `api_key_env`; literal credentials and unknown fields reject. Only loopback endpoints may use HTTP; other endpoints require HTTPS. Proxy environment variables and redirects are disabled. Credential values are resolved only for the model request, never persisted in proposals.

`POST /v1/wiki/authoring/generate` accepts `space_id`, `operation_id`, `expected_revision`, `slugs`, `selected_raw`, and a nonempty `instruction`. The service captures verified selected Raw/pages, complete index/inventory and admitted resolved schema, requiring the supplied revision to match. Context is bounded to 4 MiB and model responses to 8 MiB. No truncation or automatic retry occurs.

The model can return only `{changes, index, log_entry}`; each change has `slug`, `markdown` and optional `replacement`. The server supplies revision, selected Raw and page digest preconditions. Existing pages may change only when explicitly selected in `slugs`; full final-state validation still applies. Index and existing page text identify existing state, not independent evidence for new facts. Normal `stop`, nonempty JSON and no tool/function call or refusal are required. This validates structural/schema/source attribution, not the truth of every generated statement.

A durable unique claim precedes the network request. Results have `state=ready|failed|unsettled|abandoned` inside the response data; HTTP/envelope success alone is not generation success. Ready contains a complete validated patch and does not publish it. Submit that patch through the existing `apply` action after review. Failed has no patch. Unsettled means the attempt has no recorded outcome, not that its process is alive; it does not trigger automatic retry or takeover. A new operation ID is an explicit new model attempt and may incur another request.

`POST /v1/wiki/authoring/proposal` with `{space_id, operation_id}` only reads the committed record. Exact generate retries return existing ready/failed/unsettled records without calling the model, including after restart without model config. A changed request or actor with the same operation ID rejects. Request, actual context hash, model-config hash, state and patch are receipt-bound; owner-controlled local hashes are integrity checks, not signatures. A ready proposal may become stale after later edits; apply still enforces current CAS. `publication_performed=false` describes generation itself and is not a reconciliation of later independent apply calls.

This connects model proposals to the multi-page HTTP publishing path. Explicit unsettled-attempt recovery is described below. Scheduling, Console review, schema lifecycle and whole-installation cutover/rollback remain unfinished.

## Explicit recovery of unsettled generation

Proposal responses now include `receipt_digest`. To revoke an unsettled attempt's authority to record a result, call `POST /v1/wiki/authoring/abandon` with `{space_id, operation_id, expected_receipt, reason}`. Use the exact receipt from proposal inspection and a nonempty reason (at most 4096 UTF-8 bytes). The same admin and exact Space permissions apply; model configuration is not required.

Only `unsettled` can transition to `abandoned`. The proposal state and recovery event commit in one transaction. The new receipt binds the original request/context/model, previous receipt, abandonment command/actor/reason and timestamp. Exact command retries return the same result; a changed command or actor rejects. Ready/failed outcomes, unknown IDs and stale receipts reject without mutation. If normal settlement commits first, it wins and abandonment fails. If abandonment commits first, late model output is discarded and the generating caller receives the abandoned record, never a ready patch.

This is settlement-authority revocation, not cancellation of remote inference or proof of process death. A request already in progress (or immediately after its committed claim) may still consume provider resources. Reusing the old operation ID never generates again; to make another attempt, explicitly call generate with a new ID and a current workspace revision. Existing ready proposals are not invalidated, and submitted page edits are not rolled back by this endpoint. General scheduling, recovery automation and whole-installation rollback remain separate work.
