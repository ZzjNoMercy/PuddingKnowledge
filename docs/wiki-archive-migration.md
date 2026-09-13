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
