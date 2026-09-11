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
