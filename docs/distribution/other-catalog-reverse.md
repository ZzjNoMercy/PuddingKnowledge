# Non-core Catalog reverse disposition

Rollback of an upgraded installation never materializes legacy rows for the
non-core Platform Catalog tables: PuddingClaw never had them. The disposition
builder therefore proves, per table, how its rollback-window delta is accounted
for, and binds that proof to the frozen workspace export so a later re-cutover
can restore the exact post-migration state:

```sh
python -m knowledge_platform.distribution.other_catalog_reverse \
  --target-before /absolute/imported-catalog.sqlite3 \
  --target-after /absolute/suspended-catalog.sqlite3 \
  --frozen-export /absolute/frozen-export \
  --output /absolute/new/other-catalog-disposition.json
```

The covered tables are fixed: connectors, database connectors, source items,
sync runs, credentials, credential grants, OAuth sessions, web captures,
ingestion/processing/authoring jobs and their events, structured assets, query
results and scopes, notification events and scopes, and collection bindings.
`knowledge_catalog_schema_versions` is verified identical between the
snapshots (it is versioning, not data); a drift refuses. Any other table in
either snapshot is unknown and refuses, even when unchanged — catalogs holding
Wiki compilation, local object-store or vector-index tables need those
domains' own reverse evidence first. The core tables are out of scope here;
they are reversed by `core_catalog_reverse`.

## Disposition classes

- `verified_unchanged`: the table is canonically identical between
  `target_before` and `target_after` (same rows, same raw cells).
- `captured_in_frozen_export`: the table changed. The frozen workspace export
  must prove it captured the exact `target_after` bytes (below). Nothing is
  rewritten into the legacy candidate; the post-migration rows survive only in
  the export for a later re-cutover.

The job-queue gate is judged on the `target_after` image itself, never on the
delta: any non-terminal status row in `knowledge_ingestion_jobs`,
`knowledge_processing_jobs`, `knowledge_authoring_jobs` or
`knowledge_oauth_sessions` (in-flight grants) refuses the whole disposition,
even when the table did not change in the window. Queues must be drained at
freeze time. Terminal rows only are captured like any other domain. The event
tables carry no status; their parent job gate covers them. Sync runs are
capture-only by design.

Terminal-status allowlists (anything else, including unknown values, refuses):

| Table | Terminal statuses | Derivation |
| --- | --- | --- |
| `knowledge_ingestion_jobs` | `succeeded`, `failed` | only writers `local/files.py`, `local/read_later.py`: queued → running → succeeded/failed |
| `knowledge_processing_jobs` | `succeeded`, `failed`, `cancelled` | `catalog/processing_job_rehearsal.py:44-45` |
| `knowledge_authoring_jobs` | `published`, `failed`, `cancelled` | `catalog/authoring_job_rehearsal.py:43-51`; `waiting_for_*` still await a decision |
| `knowledge_oauth_sessions` | `consumed`, `superseded`, `failed`, `revoked`, `expired` | `local/feishu_oauth.py` (`pending`/`exchanging` are in-flight); `catalog/credential_rehearsal.py:195` (`expired`) |

Credential and grant tables are captured like other domains: they hold
Platform-side credential metadata only, and legacy-to-vault continuity is the
separate `credential_rebind` tool's job.

## Frozen-export evidence binding

The export directory must contain a canonical, private
`puddingknowledge-frozen-workspace-export/v1` `manifest.json` in state
`verified_frozen_export` with `activation_allowed=false` and
`rollback_completed=false`. The manifest's pinned `catalog.sqlite3` fact must
match the recomputed digest of `raw/catalog.sqlite3`, and that digest must
equal the `target_after` snapshot bytes. A suspended Catalog exported with live
`-wal`/`-shm`/`-journal` sidecars is refused: the raw main file is not the
complete state, so the workspace must be quiesced (queues drained, writers
fenced, WAL checkpointed) before the snapshot and export are taken. The receipt
binds the export by `frozen_export.manifest_sha256` (the manifest bytes) and
`frozen_export.catalog_sha256` (the raw Catalog bytes).

## Receipt and exact retry

The `puddingknowledge-other-catalog-reverse/v1` receipt carries
`target_before_sha256`/`target_after_sha256` (snapshot byte digests), one
entry per covered table `{table, rows_before, rows_after, digest_before,
digest_after, disposition}` with canonical row digests, the frozen-export
binding, `state=verified_other_catalog_disposition`,
`schema_versions_identical=true`, and fail-closed flags
`indexes_rebuilt=false`, `rollback_completed=false`,
`activation_allowed=false`, `installation_cutover_performed=false`. Indexes are
deliberately never materialized back; they are marked stale and the legacy
installation rebuilds them.

Snapshots are opened only as private digest-pinned copies with integrity
checks and immutable reads, and are rehashed before publication; the export
evidence is verified twice. The receipt is written with fsync and no-replace
publication. An exact retry is byte-identical and returns `idempotent=true`; a
different existing receipt refuses, and an interrupted `.part` sibling is
recovered. Failure stdout carries only the fixed
`other_catalog_reverse_rejected` code.

## Core reverse integration

`core_catalog_reverse` accepts `--other-catalog-disposition <receipt>`. When
non-core target tables changed between its snapshots, the blanket
`Unmapped target domain changed` refusal is replaced by an exact-cover check:
the changed table set must equal the set of receipt entries with a
non-refusing (`captured_in_frozen_export`) disposition, the receipt must bind
the same snapshot byte digests, and every per-table row digest and count is
recomputed against the live snapshots. Anything less keeps refusing. Without
the flag the original blanket refusal is unchanged; a receipt that covers
changes its snapshots never saw also refuses. This is disposition evidence
only — no writer is switched, no legacy rows are produced for these domains,
and it is not an installation rollback.
