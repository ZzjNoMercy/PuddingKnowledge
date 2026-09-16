# Rollback evidence assembly v1

The Knowledge-side reverse chain is four independent verified steps, each with
its own CLI and deterministic receipt: the frozen workspace export
(`local/frozen_export.py`), the non-core Catalog reverse disposition
(`distribution/other_catalog_reverse.py`), the document reverse
(`distribution/document_reverse.py`, embedding the
`distribution/core_catalog_reverse.py` receipt) and the Wiki reverse
(`distribution/wiki_reverse.py`). The Harness rollback orchestrator advances
the installation manifest to `ROLLED_BACK` bound to `sha256:<digest of a
single rollback evidence file>` and requires both products' writer
rev-assignment to commit that same digest. This assembler verifies a presented
set of the four receipts and publishes exactly that file:

```sh
python -m knowledge_platform.distribution.rollback_evidence \
  --frozen-export-manifest /absolute/frozen-export/manifest.json \
  --other-catalog-disposition /absolute/other-catalog-disposition.json \
  --document-reverse-manifest /absolute/document-reverse/manifest.json \
  --wiki-reverse-manifest /absolute/wiki-reverse/manifest.json \
  --output /absolute/new/rollback-evidence.json
```

This is VERIFY-AND-BIND only: the heavy steps are never re-run. Every artifact
must still live at the exact private directory its producing CLI committed as
`output_identity`; relocating or activating candidates is the audited domain of
`distribution/installation_path_rebind.py` after assignment. All four inputs
are required — one strict complete contract — and anything less than the full
verified set refuses.

## What is verified

For each of the four artifacts, read back as private owned files:

- canonical bytes: the file must re-encode to itself under the house canonical
  JSON discipline (sorted keys, unique keys, UTF-8, trailing newline);
- envelope: exact key sets, `format` and terminal `state`
  (`verified_frozen_export`, `verified_other_catalog_disposition`,
  `verified_inactive_documents`, `verified_inactive_wiki`);
- inert flags: every flag the artifact commits stays false —
  `activation_allowed`, `rollback_completed`, `legacy_schema_converted`,
  `credential_continuity_verified`, `indexes_rebuilt`,
  `installation_path_rebound`, `installation_cutover_performed` as applicable
  per format — plus `schema_versions_identical=true` on the disposition;
- committed outputs exist and match their pinned digests: the export's `raw/`
  and `normalized/` trees are re-inventoried against the manifest inventories
  (the raw Catalog must also be quiesced — no `-wal`/`-shm`/`-journal`
  sidecars, mirroring the disposition's own gate), the document candidate
  Catalog is rehashed against `catalog_sha256`, and the document `bodies/` and
  Wiki `brain/` trees are re-inventoried against their plan inventories.

## Verified cross-linkages

Each linkage names the field exactly as committed and the module that produces
it:

- `frozen_export.manifest_sha256` in the disposition receipt
  (`distribution/other_catalog_reverse.py`, `_verify_frozen_export`) equals
  sha256 of the presented frozen export manifest bytes.
- `frozen_export.catalog_sha256` in the disposition receipt equals the export's
  pinned raw Catalog fact `plan.source_inventory.files['catalog.sqlite3'].sha256`
  (`local/frozen_export.py`), re-verified against the raw bytes.
- `target_before_sha256` / `target_after_sha256` in the disposition receipt
  equal the snapshots the document chain consumed:
  `plan.inputs[1].sha256` / `plan.inputs[2].sha256`
  (`distribution/document_reverse.py`), which must in turn equal the embedded
  core receipt's `input_sha256[1]` / `input_sha256[2]`
  (`distribution/core_catalog_reverse.py`).
- The embedded `core_receipt` (`distribution/core_catalog_reverse.py`,
  embedded by `distribution/document_reverse.py`) is itself verified
  (`puddingknowledge-core-catalog-reverse/v1`,
  `verified_inactive_core_metadata`, inert flags false); its `output_sha256`
  must equal the document manifest's `catalog_sha256` and the recomputed digest
  of `<document output>/catalog.sqlite3`, and its `source_revision` must equal
  the document plan's.
- `plan.source_revision` of the Wiki reverse
  (`distribution/wiki_reverse.py`) must equal the document plan's
  `source_revision` — the shared source snapshot revision. The two formats
  commit different source artifact kinds (a legacy Catalog file vs a Wiki
  brain tree), so byte identity is not equatable; the revision agreement is
  the strongest committed binding.
- Operation identity: only the frozen export plan carries one
  (`plan.operation_id`, the suspension operation). It is validated and
  recorded; no other artifact format commits an operation identity, so there
  is no second carrier to bind.
- The Wiki reverse's own frozen export binding
  (`plan.inputs.current_workspace.{manifest_sha256,catalog_sha256}`,
  `distribution/wiki_reverse.py`) belongs to the separate Wiki workspace
  export, which is not an input to this assembly; it is verified by the Wiki
  reverse CLI at production time and is transitively bound by the Wiki
  receipt's own digest. Only the commitment's shape is re-checked.

## Evidence file and determinism

The evidence is canonical JSON, `puddingknowledge-rollback-evidence/v1`,
`state=verified_rollback_evidence`, carrying: the recorded `operation_id` and
`source_revision`; `artifacts` — per-artifact `{role, state, sha256}` over the
four presented files in chain order; `linkages` — the derived cross-linkage
digests (export manifest, raw and normalized Catalog, target-before/after,
document candidate Catalog); `artifact_digests` — canonical digests of the
four re-verified committed inventories; and fail-closed flags
`rollback_completed=false` (completion is the Harness orchestration plus
assignment, never this file), `activation_allowed=false`,
`installation_cutover_performed=false`, `indexes_rebuilt=false`. It contains
digests only — no secrets and no host paths beyond what the receipts already
commit — and is size-budgeted at 1 MiB.

Byte-stability is a hard requirement: the Harness orchestrator binds
`sha256` of the evidence bytes into the `ROLLED_BACK` manifest and both
products' assignment commits the same digest, so two assemblies of the same
verified set must produce the same bytes. All inputs are canonical, all
carried values are digests or committed strings, list order is fixed, and the
canonical encoder is deterministic. Publication is atomic: `.part` file,
fsync, no-replace link, directory fsync. An exact retry verifies the full set
again and finds byte-identical evidence (`idempotent=true`); a disagreeing
existing evidence file refuses; an interrupted `.part` sibling is recovered.
The presented artifacts and the document candidate Catalog are re-read once
more before publication so drift during assembly refuses.

## Operator sequence

1. Run the four step CLIs against the suspended installation: freeze and
   export the workspace (`local/frozen_export.py`), build the non-core
   disposition bound to that export
   (`distribution/other_catalog_reverse.py`), materialize the document
   candidate (`distribution/document_reverse.py`), and materialize the Wiki
   candidate (`distribution/wiki_reverse.py`, against its own workspace
   export).
2. Assemble the rollback evidence from the four receipts (this tool).
3. Hand the evidence file to the Harness rollback orchestrator; it advances
   the installation manifest to `ROLLED_BACK` bound to the evidence digest
   and drives both products' writer rev-assignment.
4. Only after assignment, place the candidates at their installation
   locations and run `distribution/installation_path_rebind.py` for the
   document candidate; activation remains a separate audited step.

Failure stdout carries only the fixed `rollback_evidence_rejected` code and
the fail-closed flags — no paths and no secret values.
