# Claw migration request generator

The Knowledge-side producer for the aggregate offline migration request. It
reads a quiesced copy of the legacy Claw Catalog plus the snapshot payload
assembled by the Harness-side producer and publishes a fully pre-verified
`puddingknowledge-migrate-from-claw-request/v2` request, so the forward chain
(`migrate_from_claw`) never starts on a bad request:

```sh
python -m knowledge_platform.distribution.claw_migration_request \
  --snapshot-root /absolute/offline \
  --catalog /absolute/offline/legacy.sqlite3 \
  --files-root /absolute/offline/payload \
  --wiki-root /absolute/offline/brain \
  --installation-id installation-1 --source-revision legacy-1 \
  --source-schema-revision claw-schema-v4 \
  --map /Users/pet/Documents/knowledge=external/knowledge \
  --output /absolute/private-request.json --receipt /absolute/private-receipt.json
```

The request has exactly the v2 contract fields: `format`, `installation_id`,
`source_revision`, `source_schema_revision`, `source_catalog`,
`source_files_root`, `source_wiki_root`, `bindings`, `original_bindings` and
`attachment_bindings`. Catalog, files root and wiki root must be absolute and
inside `--snapshot-root`; containment is verified before anything is read.
`bindings` covers exactly the `knowledge_documents` id set, `original_bindings`
covers exactly the converted-PDF representation rows, and
`attachment_bindings` covers exactly the references
`collect_attachment_references` derives from `doc_metadata` — the same
attachment selectors the document migration owns; virtual `/knowledge/...`
routes in metadata are not filesystem references and never appear.

`source_schema_revision` is a caller assertion bound into the request digest,
not proof of compatibility (see `offline-claw-migration-protocol.md`). No
consumer derives it, so the generator takes it as a parameter; for a real
legacy Home the natural value is its schema journal head, e.g.
`claw-schema-v$(SELECT MAX(version) FROM core_schema_migrations)`.

## Path mapping

Legacy rows carry absolute source paths (`storage_path` for bodies,
`doc_metadata.original_path` and attachment selectors likewise). Repeatable
`--map SRC=DST` rules re-root them into payload-relative paths. A path matches
a mapping when it equals `SRC` or starts with `SRC + '/'`, and the longest
matching `SRC` wins; overlapping prefixes are unambiguous by construction.
Every referenced absolute path must match a mapping — an unmapped reference
refuses — and two distinct source paths mapping to the same relative refuse as
a collision. `DST` and every emitted binding value must be a canonical
relative POSIX path: no absolute paths, no `..`, `.` or empty segments, no
backslashes; containment inside the files root is therefore lexical, and the
chain's symlink-free readers enforce it physically.

## Pre-verification

The Catalog copy is opened with a SQLite `mode=ro&immutable=1` URI and is never
mutated; WAL/SHM/journal sidecars refuse because the copy must be fully
checkpointed, and `knowledge_documents` must be non-empty. Before anything is
published the generator streams every bound body, original and attachment
through the same bounded readers the chain uses (128 MiB per file, 2 GiB
totals, 5000 bindings) and compares the committed digests:
`representation['body_sha256']` for converted-PDF rows, `content_sha256`
otherwise, and `'sha256:'+representation['original_sha256']` for originals,
with the same prefix handling as the document migration. PDF `size_bytes`
claims and the exact `document_tree.collect_tree` semantics — attachment
kinds, claimed facts, directory topology and the dependency graph — are
re-run against the payload. Failures fail generation, not the chain.

## Outputs and receipt

Both outputs are private 0600 files of canonical JSON (sorted keys, compact
separators, trailing newline) published atomically without replacement.
Re-running with identical inputs republishes byte-identical outputs; a
pre-existing disagreeing file refuses. The generation receipt is
`puddingknowledge-claw-migration-request-receipt/v1` with state
`verified_inactive_request`: it binds the request digest and the Catalog file
digest, records the mapping table, counts documents/originals/attachments and
verified payload bytes, and asserts `unmapped_references=0`.
`activation_allowed`, `installation_prepared` and
`complete_installation_migration` remain false — the request is inert
evidence for the migration boundary, not an activation artifact. CLI failure
prints a fixed redacted error line and exits 1.

A request whose wiki root is an entirely empty directory is valid: the Wiki
archiver preserves it as an empty evidence archive, which matches real legacy
Homes that never had Wiki content.
