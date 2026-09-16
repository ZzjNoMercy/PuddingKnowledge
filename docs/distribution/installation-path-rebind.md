# Installation path rebinding v1

A `verified_inactive_documents` candidate binds its primary document bodies to
the absolute path of the directory that materialized it. After the operator
moves the candidate tree to its final installation location, an independent
Knowledge installation exposes:

```sh
python -m knowledge_platform.distribution.installation_path_rebind \
  --candidate /absolute/new/private/candidate \
  --manifest /absolute/new/private/candidate/manifest.json \
  --output /absolute/new/private/installation-path-rebind.json
```

The candidate directory must already sit at its final location; the manifest is
the document_reverse v5 receipt and stays read-only. The rebind receipt is
written outside the candidate. The candidate is locked exclusively for the whole
run and its writer-authority identity is rechecked before the receipt publishes.

The step rewrites exactly the absolute path bindings the reverse transform
wrote with the candidate prefix, all inside the legacy `knowledge_documents`
table:

- `storage_path` — every reversed document row (document_reverse.py transform
  `body_bindings`, applied by document_reverse_plan).
- `doc_metadata.assets[*].path`, `doc_metadata.original_path`,
  `doc_metadata.multimodal.image_assets_dir` — only where the reverse
  attachment rebinding substituted a verified candidate body path
  (document_reverse.py `verified_refs` `output_path`, applied by
  document_attachment_metadata). Values still naming the user's historical
  locations are retained untouched.

`source_path` is the user's original file location and is never rewritten.
`/knowledge/...` virtual routes are root-relative and survive relocation
unchanged. No other table or column is touched.

Every rebound value must sit strictly below the plan's committed
`candidate_knowledge_root` and its root-relative suffix must be a committed
`output_inventory` file (or directory, for `image_assets_dir`); divergence,
escape or a Catalog whose bytes differ from the manifest's `catalog_sha256`
refuses. The rewrite runs in one SQLite transaction on a private digest-pinned
copy and publishes atomically (part file, fsync, rename, directory fsync) with
a digest-pinned backup hardlink held until the rebound candidate re-verifies.
Inside JSON metadata cells the rewrite is literal-for-literal: a rebound value
must occur exactly as often as it is selected, so uncommitted copies refuse.

After publication the step re-verifies end to end: every rebound field resolves
strictly under the candidate's current `bodies` root, the full body inventory is
re-read and must match the manifest's committed digests, the manifest must be
byte-stable and the Catalog digest must equal the recorded after-digest. Any
mismatch restores the original Catalog bytes from the backup and refuses,
leaving the candidate byte-identical to before the run.

The deterministic canonical receipt records `old_prefix`, `new_prefix`,
per-field rebound row counts, document count, `catalog_sha256_before` and
`catalog_sha256_after`, `bodies_verified`, `manifest_sha256` and
`installation_path_rebound=true`. `indexes_rebuilt`, `rollback_completed`,
`activation_allowed` and `installation_cutover_performed` stay false. An exact
retry finds every field already at the new prefix, re-verifies everything,
rewrites nothing and reports `idempotent=true` with a byte-identical receipt.

Interruptions are recoverable: a leftover rewrite part file is rebuilt
deterministically; a crash between Catalog publication and receipt leaves the
digest-pinned backup, from which the next run re-derives the expected Catalog
bytes and adopts them only on equality; a leftover receipt part is republished.
A candidate whose Catalog drifted from the manifest commitment without either
proof (pinned backup derivation or a matching prior receipt) refuses.

## Operator sequence

1. Obtain a `verified_inactive_documents` candidate (document_reverse v5) and
   keep it quiesced; no writer may be attached.
2. Move the candidate tree to its final installation location with a
   mode-preserving move (`mv` within one filesystem; the tree stays
   owner-private).
3. Run the rebind command above against the moved tree and its in-tree
   manifest. The run refuses if the candidate was only partially moved, edited
   or already altered outside this step.
4. The candidate is ready for the remaining audited steps (index rebuild,
   credential continuity, installation cutover), none of which this step
   performs or permits.
