# Wiki reverse materialization v1

An independent Knowledge installation now exposes:

```sh
python -m knowledge_platform.distribution.wiki_reverse \
  --source-snapshot /absolute/offline/brain-root \
  --baseline-archive /absolute/private/wiki-archive \
  --current-workspace /absolute/frozen-export \
  --output /absolute/new/private/candidate
```

All inputs are offline snapshots; live writer authority must be suspended and the
frozen workspace export obtained separately. `--source-snapshot` is the original
legacy brain root, `--baseline-archive` is the verified `prepare_wiki_archive`
output produced from it at migration time, and `--current-workspace` is the
verified frozen export root containing `raw/`, `normalized/catalog/catalog.sqlite3`
and its canonical `manifest.json`. The three inputs must be distinct and disjoint,
and the output must not overlap any of them. SQLite sees only private copies, even
when an offline header uses WAL.

The baseline archive is verified and its manifest binds the source snapshot: the
current source inventory must equal the archived inventory, so any post-migration
edit of the legacy brain rejects. The frozen export manifest must be canonical,
complete and committed (`verified_frozen_export`, plan digest, raw and normalized
inventories, normalized Catalog digest all recomputed). The export's
`raw/wiki-evidence` archive must verify to the identical baseline manifest, so a
drifting baseline rejects. When `raw/wiki-schema.json` is present it is admitted
against the evidence archive and the schema identity (`schema_id`,
`bundle_version`) is re-read from the archived brain schema; committed schema-bound
state without admitted schema evidence rejects.

The current Catalog's Wiki domain is recomputed from the archived evidence, not
trusted: exactly one Space (`Migrated Wiki`), the migrated Collection with
recomputed asset membership in archived path order, capabilities, and the archived
manifest digest, every published page Asset (title recomputed from archived bytes,
digests, metadata) and every Raw Asset with its receipt lineage. Bootstrap
timestamps and freshness observations are not reproducible and are skipped. The
platform's own validators then cross-check committed state on the private copy:
`SchemaPublication` re-verifies every schema publication against its compilation
attempts, log and Assets, and `WikiAuthoringStore.read` re-verifies the committed
authoring workspace revision and its exact Catalog projection. Unmapped domains
reject: unknown Asset source types, unknown Collections, Wiki state rows belonging
to another Space, unknown compilation statuses, succeeded compilations lacking
schema publication evidence (no faithful legacy slug), and active Wiki projections
without authoring state.

The committed delta is reversed into legacy semantics. Succeeded compilations are
replayed in committed time order; each must pass legacy lint against the admitted
schema contract and produces one legacy publish receipt under
`.puddingclaw/jobs/wiki-reverse-<digest>.json` whose `published_at` is the faithful
committed publication time. Authoring commits are replayed as a chain from the
initialized state: request, before-image, retirement and receipt commitments are
recomputed and compared per step, and every intermediate workspace must pass
legacy lint. Each commit with writes produces one publish receipt (multi-page
commits stay one receipt); each commit with retirements produces one
`page-retirement` receipt and the retired before-images under
`.puddingclaw/retired/wiki-retire-reverse-<digest>/wiki/`. Legacy retirement
semantics require an explicit replacement page; replacement-free retirements
reject. Authoring carries no committed timestamps, so receipt times are synthetic:
monotone seconds after the latest historical or compilation publication, marked
`synthetic_published_at` with a `reverse` provenance marker (`reversed: true` on
every reversed receipt). Running compilations are uncommitted and are only
counted. A zero-commit initialized authoring workspace restores the archived
bytes; the platform's initial index newline normalization is not a content
change. With no committed post-migration state at all the candidate is the
byte-identical source tree.

`wiki/index.md` and `wiki/log.md` are restored to the committed terminal state
(archived index plus sorted published slugs; archived log plus committed
entries). Pages are written with the legacy trailing-newline normalization;
pages, index and log identical to the archive are byte-copied from the source
snapshot, as are all historical `.puddingclaw` receipts and retired archives. A
new receipt colliding with archived evidence rejects. Wiki directories emptied by
retirement are pruned; directories already empty in the source are preserved.

The output is `brain/` plus `manifest.json` under an exclusive writer lock. The
manifest binds input paths and content hashes, output identity, the complete
output inventory, the delta and receipt metadata. `copying` state supports exact
retry after interruption, including SIGKILL mid-copy; partial `.reverse-part`
files are private and adopted only by the matching plan. Completed missing,
altered or foreign entries reject without repair. Source, baseline, export,
evidence and output are all rechecked before manifest publication.

This is `verified_inactive_wiki`. Raw domain bytes and the schema bundle are
unchanged; the candidate is never activated and no writer is switched.
`activation_allowed`, `rollback_completed`, `credential_continuity_verified`,
`indexes_rebuilt` and `installation_path_rebound` stay false; relocating or
activating the candidate requires a separate audited step. Budgets mirror the
archive contract (50k files, 256 MiB per file, 2 GiB total, 32 MiB metadata, 256
MiB Catalog) plus 500 compilations, 200 authoring commits and 32/256 MiB
per-commit/total committed payloads.

## Wiki-absent attestation v1

An installation that never had a Wiki domain (an empty legacy `llm-wiki`, zero
Wiki Spaces in its Catalog) has no brain to materialize, so the active path
above cannot produce its receipt. For that case the installation exposes a
verifiable proof of absence as a separate, explicitly chosen step — never a
fallback when the active Wiki reverse rejects:

```sh
python -m knowledge_platform.distribution.wiki_reverse_absent \
  --current-workspace /absolute/frozen-export \
  --source-revision legacy-1 \
  --output /absolute/new/private/attestation
```

`--current-workspace` is the same verified frozen export the active path
consumes and it is re-verified identically (canonical manifest, plan digest,
raw and normalized inventories, normalized Catalog digest all recomputed).
`--source-revision` is the operator-committed source snapshot revision, bound
under the same token rule the baseline archive's revision passes at migration
time, so the rollback assembly's cross-chain revision agreement still holds.
The output must not overlap the export.

The normalized Catalog must then prove empty, fail-closed: integrity check,
every table name against the known schema allow-list (any unknown table is
schema drift and refuses), the active path's exact core selections
(`knowledge_spaces`, `knowledge_datasets`, `knowledge_assets`) must return zero
rows, and every Wiki state table the active path inspects
(`knowledge_local_wiki_compilations`, `knowledge_local_wiki_schema_pages`,
`knowledge_local_wiki_schema_log`, `knowledge_wiki_authoring_state`,
`knowledge_wiki_authoring_pages`, `knowledge_wiki_authoring_commits`) must be
absent or empty. Any Wiki row anywhere refuses, as does any `wiki-evidence` or
`wiki-schema.json` member in the export's `raw/` tree. The core tables must
exist with the active path's columns; missing tables refuse as drift, and
present-but-empty Wiki state tables still attest.

The output is `manifest.json` alone under an exclusive writer lock — there is
no `brain/`, because there is nothing to restore. The receipt is
`puddingknowledge-wiki-reverse-absent/v1`, state `verified_absent_wiki`,
committing the `current_workspace` binding (`path`, `manifest_sha256`,
`catalog_sha256`, the same shape as the active plan's
`inputs.current_workspace`), the source revision, the output identity and the
exact absence evidence: the inspected table set with per-table
`present`/`rows: 0` proofs and the two export-member absence flags. The same
five inert flags stay false. Publication is canonical, budgeted and atomic; an
exact retry finds byte-identical receipt bytes and reports `idempotent`, a
changed or foreign receipt refuses without repair, and the export is re-read
before publication so drift refuses. Failure stdout carries only the fixed
`wiki_reverse_absent_rejected` code and `activation_allowed=false`.


Known limits: schema bundle updates during the installation are not reversed
(`schema_unchanged` only); legacy unprojected authoring state
(`catalog_projected=0`) rejects rather than being projected retroactively;
compiled Collection `asset_ids` order and Collection freshness are not
recomputed; non-Wiki Catalog domains are out of scope for this tool; synthetic
authoring receipt times are provenance-marked approximations, and the historical
receipt timeline is preserved only through the archived receipts themselves.
