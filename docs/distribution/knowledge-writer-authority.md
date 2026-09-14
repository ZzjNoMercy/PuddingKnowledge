# Existing Knowledge workspace writer authority

The independently installed Knowledge distribution owns this protocol. It has
no Harness source import or runtime dependency. Enrollment records an existing
local workspace's `knowledge_catalog` and `connector_jobs` writers; it does not
activate a migrated installation, reassign a domain, thaw a workspace, or satisfy
the complete active-installation revision requirement in specification 11.20.
Unenrolled independent workspaces retain existing behavior.

```sh
python -m knowledge_platform.local.writer_authority enroll \
  --state-dir /absolute/private/owned-workspace \
  --authority /absolute/new-private/authority \
  --operation-id enroll-existing-1
python -m knowledge_platform.local.writer_authority status \
  --state-dir /absolute/private/owned-workspace
python -m knowledge_platform.local.writer_authority suspend \
  --state-dir /absolute/private/owned-workspace --operation-id suspend-existing-1
```

Enrollment requires an existing valid owned manifest and private workspace,
usually mode 0700. It rejects unexpected root files and never silently repairs a
legacy public workspace's permissions. The disjoint authority root holds a
permanent private lock and a canonical digest-checked journal. The fixed private
`.workspace-authority-v1.json` binding records both directory identities, exact
workspace manifest digest and enrollment operation. Its publication part denies
normal startup; a complete matching part can be retried. Control files are never
placed inside Wiki evidence, blobs, processing, or Catalog tables.

Revision 0 records the workspace's existing writers. Suspension first completes
the owned-workspace freeze, then exclusively commits revision 1 with both writers
set to null and a freeze receipt commitment. Failure between those steps leaves
the workspace frozen and can be retried with the same operation. Completed
suspension retries revalidate the marker; removed committed markers are not
recreated. The suspended journal itself denies startup even if a disposable test
removes the freeze marker. Malformed/missing journals, moved roots, changed
manifest bytes and partial binding publication reject before business startup.

Normal owned-workspace admission holds its existing exclusive workspace lease
and an authority shared lease. The CLI passes both to WikiQueueWorker. A Worker
for an enrolled Catalog requires both descriptors and verifies they correspond
to that actual Catalog's workspace and authority locks. The Catalog must be a
private owned regular file without symlinks or hardlinks. It duplicates them before
thread startup; partial duplication or thread-start failure releases only the
duplicates. A bounded close can return while the Worker remains alive: both
leases remain held through its last local settlement write, even after the API
workspace context closes. The same rule prevents a concurrent authority change.

Process death still releases all local descriptors. It does not cancel remote
inference or convert an `unsettled` proposal into a successful settlement; existing
explicit recovery/no-automatic-retry rules remain. A model or SQLite failure can
leave an unsettled record according to that existing protocol. Thread/process
exit is not represented as proof of model cancellation or completed business work.

All locks use nonblocking POSIX flock. Lock contention rejects and releases;
normal shutdown must stop writers before suspension. Arbitrary nonparticipating
code, permanent binding/lock deletion, hostile same-user path replacement and
external stores are outside the cooperative fence. This is not an installation
CUTOVER or rollback receipt. Cross-product revision switching, verified thaw,
lossless reverse data migration, credential continuity and product/release
acceptance remain unfinished.

Interrupted journal replacement may retain private `.journal.json.tmp-*` files in
the authority directory. They are diagnostic leftovers, never active revisions;
no automatic deletion or exact-empty-directory claim is made.
