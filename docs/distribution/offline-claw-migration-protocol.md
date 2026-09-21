# Offline Claw migration process protocol v1

An installed Knowledge runtime exposes:

```sh
python -m knowledge_platform.distribution.migrate_from_claw \
  --source-snapshot /absolute/offline \
  --request /absolute/private-request.json --output /absolute/new-delegate-output
```

The request is a private, regular, unlinked JSON file, at most 1 MiB. It has
exactly these fields; duplicate or unknown keys are rejected:

```json
{
  "format": "puddingknowledge-migrate-from-claw-request/v1",
  "installation_id": "installation-1",
  "source_revision": "offline-1",
  "source_schema_revision": "legacy-1",
  "source_catalog": "/absolute/offline/catalog.sqlite3",
  "source_files_root": "/absolute/offline/files",
  "bindings": {"document-id": "docs/body.md"}
}
```

The caller supplies an immutable offline snapshot. Schema revision is a caller
assertion bound into the request digest, not proof of whole-installation
compatibility. Actual document schema and content are verified by the existing
document migration converter. Paths are explicit; credentials are not discovered.

Requests are ordinarily produced by the Knowledge-side generator described in
`claw-migration-request.md`, which derives a fully pre-verified v2 request
(including `original_bindings` and `attachment_bindings`) from a quiesced
legacy Catalog copy and the snapshot payload.

Success stdout is one `puddingknowledge-migrate-from-claw-receipt/v1` JSON object:
`request_digest` binds the exact request bytes, `source_snapshot_identity` binds
the approved absolute snapshot root independently supplied by the orchestrator.
Knowledge rejects Catalog/files outside that root. `artifacts` maps portable relative
paths to SHA-256 digests, and `covered_domains` / `pending_domains` state scope.
The receipt is also stored in `receipt.json`; converted data is in `candidate/`,
which can initialize the persistent local runtime with `--document-migration`.
Harness treats the request as opaque and validates the process receipt and bytes;
it never imports Knowledge source or interprets Catalog records.

Current coverage is document Catalog and blobs. Other Catalog domains, Wiki,
indexes and knowledge credentials remain pending. Every receipt states
`verified_inactive_partial`, `installation_prepared=false`,
`activation_allowed=false`, `writer_fence_verified=false`, and
`credential_rebind_required=true`. This is not the full installation Migration
Manifest, compatibility gate, source snapshot acquisition, or CUTOVER authority.

## Migration byte budgets

The byte budgets were originally sized for small synthetic fixtures. Spec §11.20
item 10 makes a rehearsal against a real PuddingClaw data copy a hard acceptance
gate, and the measured real Home exceeded the old limits: the largest single
file is a 158 MB original PDF (the largest Markdown body is 67 MB), referenced
document bytes total ≈268 MB, Wiki sessions total
≈300 MiB and the Catalog is 50 MB. The budgets are now uniform across the
document, Wiki archive and Catalog domains: 256 MiB per migration file, 2 GiB
per domain or receipt total, and 256 MiB per Catalog normalization/snapshot
file (512 MiB per raw sidecar bundle). The source-snapshot envelope enforced by
the Harness orchestrator (2 GiB per file, 16 GiB total) remains the outer
denial-of-service gate.

Retries require identical request bytes and re-run source/target verification.
A process lock serializes output. A crash after candidate publication resumes
before receipt publication; immutable plan/receipt publication never replaces an
existing competing file. Unknown files, changed source, altered artifacts and
changed receipts fail closed. Failure stdout contains a fixed error code and no
request paths or secrets. The explicit installed executable is a trusted local
provider; the receipt is not a cryptographic attestation of arbitrary executables.

## Normalize raw SQLite sidecars without modifying the snapshot

When the approved Catalog has WAL, SHM or rollback-journal siblings, Knowledge
copies the bounded bundle into `normalization/.work` under its own output. Only
that copy is opened with SQLite. Backup materializes a separate file, with
trusted schema disabled, a page/byte budget, progress deadline and integrity
check. The committed raw snapshot is never opened as a SQLite connection.
A stopped writer's hot rollback journal is recovered on the copy; committed WAL
pages are included in the normalized Catalog. Source member digests are checked
before/after copy and after materialization, then again before the receipt.

The directory has its own lock and immutable source plan. Interrupted work can
be rebuilt only after its known private entries are checked. Same-inode partial
link publication pairs resume. A published report commits the normalized Catalog
bytes; completed artifacts are never silently repaired. Source bundle changes,
unknown work files and modified completed artifacts reject replay. Catalog and
report names are separate from raw work-copy names, including when the original
Catalog is named `catalog.sqlite3`.

The v1 receipt's top-level fields stay unchanged. Its existing `artifacts` map
additionally covers `normalization/plan.json`, `normalization/report.json` and
`normalization/catalog.sqlite3`. The report binds the source plan digest and
normalized file digest/size; no host source paths or secret values are printed.
Inputs with no sidecars keep the existing direct offline converter and artifact
layout for compatibility. Normalization is limited to 256 MiB per member,
512 MiB per raw bundle and a 5 second SQLite materialization budget.

This does not acquire source-writer authority or migrate other Catalog domains.
It consumes an immutable approved snapshot, and all existing partial/inactive
flags remain in force. The normalized legacy Catalog is intermediate evidence,
not the target Knowledge Catalog and never a Harness database.

Document requests may additionally supply `original_bindings`,
`attachment_bindings` and `virtual_roots` objects. They are passed only to the
document converter; Wiki archiving retains its separate input contract.
`virtual_roots` is a bounded list of `virtual_prefix`/`relative_root` pairs
that rebinds absolute in-body references in the product's virtual namespace
onto snapshot-relative roots during dependency collection; undeclared absolute
references refuse. Document migration v3
commits a source-relative `resources` tree, and the outer receipt includes those
file hashes. The full tree, including empty directories, is verified before
receipt publication. Source-reference mapping and package/workspace versions are
described in `forward-document-dependencies.md`; this remains an inactive partial
migration receipt with no installation writer assignment.
