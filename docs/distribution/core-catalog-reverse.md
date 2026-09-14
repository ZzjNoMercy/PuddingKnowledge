# Core Catalog metadata reverse conversion

The independent Knowledge distribution can construct an old-schema SQLite
candidate from a legacy source snapshot, its proven forward Catalog baseline,
and a current normalized Catalog snapshot:

```sh
python -m knowledge_platform.distribution.core_catalog_reverse \
  --source-snapshot /absolute/legacy.sqlite3 \
  --target-before /absolute/imported-catalog.sqlite3 \
  --target-after /absolute/frozen-export/normalized/catalog/catalog.sqlite3 \
  --source-revision legacy-1 \
  --output /absolute/new/legacy-candidate.sqlite3
```

All three inputs must be separate offline databases without SQLite sidecars. The
current database should come from the frozen workspace exporter. This command
consumes snapshot evidence; it does not acquire or revoke live writer authority.
SQLite opens only private copies. The bounded existing snapshot inspector rejects
executable schema, virtual tables, generated columns and AUTOINCREMENT tables.

The converter recomputes the complete original spaces/assets/datasets projection
from the actual old schema and requires exact baseline equality. Every row in both legacy core tables must
have been projected; partial row ownership inside either table is not supported. Existing mapped
space/document metadata changes and deletions are reversed into old rows; source
columns not represented in the projection are retained. Portable new `space_<id>`
identities can become new knowledge bases. Dataset rows have no separate legacy
storage: current datasets must remain exactly derivable from the reversed bases
and documents, including membership, ordering, capabilities and source revision.

Recursive metadata overlays preserve original secret values whose redacted
projection did not change. Ambiguous changes to containers with redacted values
are refused. The old-schema candidate is forward-projected again and compared
against every current core field. Unsupported content digest/revision, URI,
permissions, provenance or metadata changes cannot disappear silently. Declared
legacy string widths are checked even though SQLite does not enforce VARCHAR
widths. Existing unmodified legacy NULL/metadata values remain intact.

The candidate keeps the original schema and non-core old tables. Replacement is
transactional with cascading deletion disabled; the final complete foreign key
check rejects dangling dependencies. Schema, all non-owned rows, reconstructed
legacy rows and the forward projection are checked before no-replace publication.
Inputs are rehashed before publication; existing output is never overwritten.
The JSON receipt reports hashes and counts without document values or secrets.

This is `verified_inactive_core_metadata`, not a runnable rollback installation.
New documents, changed bodies and physical file layout require a separate verified
identity/file materializer and currently reject. Other changed target domains also
reject. A metadata deletion does not remove its old body from a future Home.
Credential continuity, installation revision switching and audited thaw remain
unimplemented here. Existing size/row limits apply. POSIX no-replace publication
can leave private temporary artifacts after process death; this command does not
claim resumable installation checkpoints. Activation and rollback completion are
always false.

The checked-in test fixture `tests/fixtures/legacy-core-reverse-schema.sql` was
generated from the current Claw `knowledge.models.Base.metadata` (18 tables).
It exercises old-schema construction without importing Claw at runtime. It is
a compatibility fixture, not evidence of a released legacy binary activation.
