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
`activation_allowed` remain false. The aggregate Harness migration protocol and
persistent workspace do not yet consume this archive.

The next migration step must build an owned Catalog/query projection from this
verified evidence, preserve original identities and Raw consumption lineage, and
prove independent restart and compile/publish behavior before changing those
claims. Installation cutover and rollback remain separate requirements.
