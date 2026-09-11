# SQLite Catalog snapshot with a real write fence

```sh
python -m knowledge_platform.distribution.catalog_snapshot --source /absolute/catalog.sqlite3 --output /absolute/new-snapshot.sqlite3 --timeout-seconds 5
```

This installed Platform command opens the explicit source in read-write mode and
holds a SQLite `BEGIN IMMEDIATE` transaction. Existing writers must finish before
it acquires the lock; new SQLite writes are blocked while the snapshot is taken.
A separate read-only transaction uses SQLite's backup API, so committed WAL data
is included. Raw copying of the main database file is intentionally not used.
The snapshot is checked, switched to DELETE journal mode, synced and published
with no-replace hard-link semantics. Existing output is never replaced.

The source lock is rolled back and released before returning. Ordinary writers
can resume, and later changes are absent from the snapshot. Returned evidence
therefore states `catalog_fenced_during_snapshot=true`, `writer_fence_held=false`,
`installation_writer_fence_verified=false` and `activation_allowed=false`.
This is not a persistent cutover fence, an installation PREPARED state, or a
cross-store snapshot of files, Vault, jobs, sessions, or external indexes.

The source must be an existing regular file with no symbolic/hard links. Path
ancestors and existing SQLite sidecars are checked. The open database identity is
checked after acquiring the lock and before releasing it; replacing the source
file rejects publication. This assumes normal SQLite writers, not a filesystem
sandbox against another process that can replace directories or databases.

Budgets: 64 MiB main database, individual sidecars and resulting database; timeout
is positive and at most 60 seconds. Snapshot pages are checked during backup.
Lock contention and verification failures remove temporary output and release
connections. Process death releases the SQLite lock, but may leave a private
`.knowledge-snapshot-*` temporary file; a fresh output attempt can proceed without
adopting that file. Snapshot files are mode 0600 and may contain credentials or
private content; keep them private. CLI reports only digest, sizes, schema version
and fixed status fields, not source paths or rows.

This command is usable independently of Harness. A future installer orchestrator
must bind the snapshot digest to migration checkpoints, obtain the remaining
domain fences and preserve the original installation for rollback. A completed
snapshot alone never authorizes active-writer switching.

Tests use real SQLite connections in rollback-journal and WAL modes, verify
writes block while fenced and resume after release, verify no post-snapshot rows,
reject busy writers and source replacement, and check failure cleanup and report
redaction. No production database is used.

This primitive accepts generic SQLite schemas to support different legacy Catalog
versions. It performs a physical integrity check, not a Knowledge schema or
foreign-key migration compatibility check. `catalog_schema_verified=false` makes
that distinction explicit. Migration adapters must validate required tables,
versions, ownership and references before importing this snapshot. External host
processes with filesystem write access can tamper with sidecars; the SQLite lock
is not a general filesystem tamper barrier.
