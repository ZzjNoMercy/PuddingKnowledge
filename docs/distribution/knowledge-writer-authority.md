# Knowledge workspace writer authority

The independently installed Knowledge distribution owns this protocol. It has
no Harness source import or runtime dependency. Enrollment records an existing
local workspace's `knowledge_catalog` and `connector_jobs` writers; suspension
freezes them; reassignment commits an `assigned` revision bound to a verified
installation migration manifest; audited thaw retires the freeze marker only
for a workspace assigned to this Knowledge. It still does not activate a
migrated installation or satisfy the complete active-installation revision
requirement in specification 11.20: cross-product CUTOVER/rollback
orchestration remains with the Harness distribution. Unenrolled independent
workspaces retain existing behavior.

```sh
python -m knowledge_platform.local.writer_authority enroll \
  --state-dir /absolute/private/owned-workspace \
  --authority /absolute/new-private/authority \
  --operation-id enroll-existing-1
python -m knowledge_platform.local.writer_authority status \
  --state-dir /absolute/private/owned-workspace
python -m knowledge_platform.local.writer_authority suspend \
  --state-dir /absolute/private/owned-workspace --operation-id suspend-existing-1
python -m knowledge_platform.local.writer_authority assign \
  --state-dir /absolute/private/owned-workspace --operation-id suspend-existing-1 \
  --manifest /absolute/private/manifest.json --writer puddingknowledge
python -m knowledge_platform.local.writer_authority assign \
  --state-dir /absolute/private/owned-workspace --operation-id suspend-rollback-1 \
  --manifest /absolute/private/rolled-back.json --writer puddingclaw \
  --rollback-evidence /absolute/private/reverse-evidence.json
python -m knowledge_platform.local.writer_authority thaw \
  --state-dir /absolute/private/owned-workspace --operation-id suspend-existing-1 \
  --manifest /absolute/private/manifest.json
```

Enrollment requires an existing valid owned manifest and private workspace,
usually mode 0700. It rejects unexpected root files and never silently repairs a
legacy public workspace's permissions. The disjoint authority root holds a
permanent private lock and a canonical digest-checked journal. The fixed private
`.workspace-authority-v1.json` binding records both directory identities, exact
workspace manifest digest and enrollment operation. Its publication part denies
normal startup; a complete matching part can be retried. Control files are never
placed inside Wiki evidence, blobs, processing, or Catalog tables.

## Journal revisions

Revision 0 records the workspace's existing writers. Suspension first completes
the owned-workspace freeze, then exclusively commits an odd `suspended`
revision with both writers set to null and a freeze receipt commitment. Failure
between those steps leaves the workspace frozen and can be retried with the
same operation. Completed suspension retries revalidate the marker; removed
committed markers are not recreated. The suspended journal itself denies
startup even if a disposable test removes the freeze marker.
Malformed/missing journals, moved roots, changed manifest bytes and partial
binding publication reject before business startup.

Revisions at and above 2 alternate strictly: an even revision is `assigned`,
an odd one is `suspended`; the chain never regresses or skips. An `assigned`
event carries the seven base keys (`revision`, `previous`, `operation_id`,
`state`, `writers`, `freeze_receipt_sha256`, `sha256`) plus three commitments:
`active_installation_revision` (`sha256:`-prefixed digest of the migration
manifest bytes), `migration_manifest_sha256` (the same bare digest) and
`rollback_evidence_sha256` (bare digest of the reverse-candidate evidence,
required exactly when assigning back to `puddingclaw`, null otherwise). Both
writer domains are always equal and either `puddingclaw` or
`puddingknowledge`, and the assigned freeze commitment repeats the superseded
suspension's, keeping the marker bound to the chain. Older binaries that only
accept at most two events refuse such journals; that rejection is the intended
fail-closed behavior because a revision-2-or-later workspace is no longer
theirs to write.

## Reassignment (assign)

Assignment requires the journal head to be `suspended` under the same
operation; the freeze marker bytes are revalidated against the suspension
commitment before committing. The manifest is a private bounded file parsed
without canonical-form requirements and validated structurally against the
installation migration manifest contract owned by this repository
(`docs/knowledge-platform/installation-migration-manifest.schema.json`,
validator `knowledge_platform.distribution.installation_manifest_validator`).
Forward assignment to this Knowledge requires manifest state `PREPARED` and
carries no rollback evidence. Rollback assignment to `puddingclaw` requires
state `ROLLED_BACK` and a `--rollback-evidence` file whose byte SHA-256 equals
the manifest's `rollback_evidence_digest`. The committed revision binds the
raw manifest bytes, not their canonical re-encoding. Retrying with the same
inputs is exact; retrying with a different operation, writer, manifest or
evidence rejects as an assignment change.

## Audited thaw

The freeze marker is never silently deleted. Thaw holds the exclusive
workspace and authority leases, then requires the journal head to be
`assigned` to this Knowledge under the given operation with the supplied
manifest matching both head commitments. It first writes a private
`thaw-receipt-rev<N>.json` into the authority directory (binding the
operation, the head revision digest, the freeze commitment and both manifest
commitments), fsyncs it, then atomically retires the marker by renaming it to
`freeze-marker-rev<N>.json` inside the same authority directory and fsyncs
both directories. A rename across devices fails closed. A crash leaves one of
three states — neither artifact, receipt only, or receipt plus retired marker
— and each retries exactly; a conflicting live marker, a tampered receipt, a
tampered retired marker, or a missing receipt for a retired marker all reject
without automatic repair. Only a workspace assigned to this Knowledge has a
thaw path; suspended and assigned-away workspaces stay frozen.

## Runtime admission

Normal owned-workspace admission holds its existing exclusive workspace lease
and an authority shared lease. The head states `existing_writer` and
`assigned` to `puddingknowledge` admit writes; `suspended` and `assigned` to
`puddingclaw` deny them, and the journal remains authoritative even when the
freeze marker is gone. The CLI passes both leases to WikiQueueWorker. A Worker
for an enrolled Catalog requires both descriptors and verifies they correspond
to that actual Catalog's workspace and authority locks. The Catalog must be a
private owned regular file without symlinks or hardlinks. It duplicates them
before thread startup; partial duplication or thread-start failure releases
only the duplicates. A bounded close can return while the Worker remains
alive: both leases remain held through its last local settlement write, even
after the API workspace context closes. The same rule prevents a concurrent
authority change.

Process death still releases all local descriptors. It does not cancel remote
inference or convert an `unsettled` proposal into a successful settlement;
existing explicit recovery/no-automatic-retry rules remain. A model or SQLite
failure can leave an unsettled record according to that existing protocol.
Thread/process exit is not represented as proof of model cancellation or
completed business work.

All locks use nonblocking POSIX flock. Lock contention rejects and releases;
normal shutdown must stop writers before suspension. Arbitrary nonparticipating
code, permanent binding/lock deletion, hostile same-user path replacement and
external stores are outside the cooperative fence.

## CLI receipts

Every successful action prints the pinned cross-product receipt shape
(`format`, `status`, `journal`, `installation_cutover_performed`) consumed by
the Harness writer barrier; the journal's last event reports the head
revision, state and writers for `status`. `installation_cutover_performed`
stays false: assignment and thaw are local authority acts, not the
cross-product CUTOVER, which the Harness orchestrator publishes separately.
Failures print `activation_allowed: false` and exit nonzero. Interrupted
journal or thaw-receipt replacement may retain private `.*.tmp-*` files in the
authority directory. They are diagnostic leftovers, never active revisions; no
automatic deletion or exact-empty-directory claim is made. Cross-product
revision switching orchestration, verified thaw of the peer product, lossless
reverse data migration, credential continuity, cross-machine fencing and
product/release acceptance remain unfinished.
