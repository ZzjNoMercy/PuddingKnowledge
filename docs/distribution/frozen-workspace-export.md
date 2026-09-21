# Frozen Knowledge workspace export

The independent Knowledge distribution provides a consistent local export for
reverse migration. It requires an already enrolled and suspended workspace with
an exact suspension operation. A freeze marker alone is insufficient.

```sh
python -m knowledge_platform.local.frozen_export \
  --state-dir /absolute/private/owned-workspace \
  --output /absolute/new-private/export \
  --operation-id rollback-stop-1
```

The workspace ownership lock is held exclusively and its writer authority lease
is held shared throughout capture. The command validates the suspended revision,
its freeze receipt, manifest and root identities before copying. It never opens
the source database with SQLite: all source reads are byte reads. Root entries
must belong to the owned-workspace contract; unknown files reject. All copied
files/directories must be private, owned and unlinked. Reserved `.export-part`
paths reject. Existing non-private data must be migrated explicitly beforehand;
the exporter does not silently change source permissions.

Output contains:

- `raw/`: byte-preserved workspace, including Catalog/WAL/SHM, Wiki, blobs,
  schema/evidence, processing state and existing control records.
- `normalized/catalog/catalog.sqlite3`: SQLite backup materialized from private
  copies of the raw Catalog family, including committed WAL frames, with DELETE
  journaling and an integrity check.
- `normalized/retrieval-traces/catalog.sqlite3`: same normalization when the
  optional retrieval trace database exists.
- `manifest.json`: source inventory, enrollment/suspension commitments, output
  identity, normalized inventories and reports; metadata includes paths and
  digests, not document or database contents.

Raw control bindings retain original identities. This export is not a relocated
runnable workspace and does not authorize startup. Source SQLite sidecars remain
unchanged; normalization opens only its own disposable copies. At completion the
source and raw inventories are checked again. A copying manifest supports exact
retry after interruption. Completed missing/tampered raw, normalized files or
reports reject before any attempted regeneration. Atomic manifest replacement
may leave private diagnostic temporary files that are never active state.

The command inherits existing limits: 256 MiB per raw file, 2 GiB raw aggregate,
50,000 raw entries; each SQLite family is bounded by the normalization component
(256 MiB per member, 512 MiB bundle, five-second materialization). Full legacy
schema conversion, reverse file-layout mapping, credentials, unified installation
revision and thaw are not implemented by this exporter. Both-product suspension
must still be coordinated by the Harness writer barrier before installation
reverse migration. `workspace_writers_suspended` describes this workspace only;
activation, rollback completion and credential continuity remain false.

This is cooperative POSIX fencing and local integrity evidence, not a signature
against an owner rewriting all control artifacts. Nonparticipating writers and
hostile same-user path/lock replacement remain outside the fence. Existing raw
files and normalized databases are separate representations; only the normalized
SQLite files are suitable for the existing offline same-schema reverse builder.
That builder still requires verified source/baseline compatibility and an explicit
table allowlist; normalized output does not prove legacy schema compatibility.
