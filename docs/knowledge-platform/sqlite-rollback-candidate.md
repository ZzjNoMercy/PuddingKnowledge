# Offline SQLite rollback candidates

The previous installation shadow incorrectly deleted post-cutover objects from
both modeled installations and called that lossless. That assertion is invalid.
The corrected ID model retains new identities in the restored source and retains
the target for recovery. It reports `identity_preserving=true` and
`lossless=false`: IDs alone cannot prove payloads, revisions, updates, deletions
or writer fencing. The shadow RC matrix keeps stateful rollback blocked, even
when an input report self-asserts successful replay. Earlier shadow outputs are
historical evidence, not proof of a lossless installation rollback.

The independent package now provides a real database candidate builder:

```sh
python -m knowledge_platform.distribution.rollback_cli \
  --source-snapshot /absolute/offline/source.db \
  --target-before /absolute/offline/target-before-cutover.db \
  --target-after /absolute/offline/target-after-freeze.db \
  --output /absolute/staging/rollback-candidate.db \
  --table knowledge_assets --table knowledge_datasets
```

Use the installed Platform environment's Python. The table arguments are an
explicit ownership allowlist, not a complete example migration inventory. Any
changed target table omitted from that list is refused. This command does not
discover or access the user's actual Claw installation automatically.

The source snapshot is the old mixed database; its non-owned tables (including
Harness data) are retained. Target-before is the baseline at cutover and
target-after includes changes made after cutover. Owned schemas, indexes and
source/baseline rows must agree; target schemas/indexes must not change. The
candidate contains target-after owned rows, including inserts, updates and
deletes, while all unowned source rows and schema remain exactly intact.

Originals are opened only as files and copied into private temporary storage.
SQLite reads those copies with `mode=ro&immutable=1`, so WAL-mode input headers
cannot create sidecars beside originals. Existing WAL/SHM/journal files are
refused: the caller must supply complete offline snapshots. Input file digests
are rechecked before publication; a detected change refuses the candidate.
This does not establish that production writers have been fenced. Nor does it
prove that separate input snapshots correspond to one installation revision.

Before publication, the builder checks full logical content, schema/indexes,
foreign keys and SQLite integrity. A candidate is published with a no-replace
hard link in the destination directory, followed by a directory fsync. Existing
output is never overwritten. Temporary data uses a private directory and 0600
files. Reports contain digests and per-table insert/update/delete counts, not
raw rows, SQL or credentials.

Current bounds are 256 MiB per input and logical row set, 100,000 rows per database,
256 owned tables and 8 MiB SQLite values. Owned tables need non-null unique primary
keys. Triggers, views, virtual/generated-column tables, AUTOINCREMENT schemas and
schema transformations are unsupported and rejected. Foreign-key actions that
change unowned rows also cause the final comparison to reject publication.
There is no relaxed fallback that silently drops unsupported data.

Success is `candidate_verified`, with `activation_allowed=false`,
`installation_cutover_performed=false` and `writer_fence_verified=false`.
The candidate is not automatically opened by either product. Installation
orchestration must still freeze all writers, coordinate Catalog/files/vector
snapshots, securely rebind credentials, validate transformations, record a
versioned manifest and atomically switch the active installation. Session delta
migration, post-cutover blob/index changes, resume checkpoints and actual
installation rollback remain unfinished. This primitive cannot make the full
RC rollback gate pass.

## Reproducible validation

From `backend`:

```sh
uv sync --locked --all-extras
PYTHONPATH=. .venv/bin/python -m pytest -q \
  tests/test_knowledge_platform_installation_migration.py \
  tests/test_knowledge_platform_rc_validation.py \
  tests/test_knowledge_platform_sqlite_reverse_delta.py \
  tests/test_knowledge_platform_rollback_cli.py
```

Tests cover current migrated Platform Catalog schema, real row changes, source
Harness-table preservation, BLOBs, null keys, unowned changes, index changes,
foreign keys, output conflicts, stable repeated output digests, WAL headers,
input changes during construction and the false-lossless regression. Installed
CLI validation runs from outside the source directory without importing Claw.
