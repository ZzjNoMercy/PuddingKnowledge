# Persistent workspace freeze

`knowledge_platform.local.workspace_freeze` records a durable, path-free
freeze receipt for an already initialized persistent state directory. It
requires an existing valid `workspace.json`, takes the ordinary workspace
exclusive lock, and never initializes or rewrites the Catalog or manifest.

```sh
PYTHONPATH=backend python -m knowledge_platform.local.workspace_freeze \
  --state-dir /absolute/path/to/state \
  --operation-id freeze-1
```

The marker is `.workspace-freeze-v1.json`. Publication uses a private bounded
`.part` file, file and directory fsyncs, and a hard-link publication step. A
matching retry is idempotent. Any conflicting, public, symlinked, or altered
partial state is rejected and retained for diagnosis. Every successful or
failed CLI result includes `activation_allowed: false` and omits host paths.

The receipt also binds the exact workspace manifest bytes. Domain loaders run
before the first marker is created; exact retries verify this commitment without
asking a loader that intentionally rejects freeze markers to reopen the frozen
workspace. The receipt is a historical freeze record, not a fresh payload audit.

The normal local CLI passes its workspace lock descriptor to the Wiki queue
worker. The worker duplicates the same open-file description before starting
and closes it only after its final settlement/write has completed. A bounded
worker close can return after one second while retaining admission; neither
freezing nor another workspace process can proceed until that worker exits.
Process termination releases all descriptors but may leave an unsettled model
proposal, which is never interpreted as cancellation or automatically retried.

There is no thaw command or automatic target-to-source rollback here. A later
installation orchestrator must freeze both products, verify data/credential
reverse conversion and switch the audited installation revision. Arbitrary code
that skips workspace admission, external stores and Home/lock replacement are
outside the cooperative local-runtime guarantee.
