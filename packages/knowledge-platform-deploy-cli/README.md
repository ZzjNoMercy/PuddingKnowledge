# Knowledge Platform Deploy CLI

This is the independent Platform distribution control plane. It does not
import PuddingClaw or start the legacy Electron/backend processes.

```bash
knowledge-platform init --home /absolute/path/to/platform-home --json
knowledge-platform status --home /absolute/path/to/platform-home --json
knowledge-platform health --home /absolute/path/to/platform-home --json
knowledge-platform plan deploy --home /absolute/path/to/platform-home --json
knowledge-platform plan infrastructure --home /absolute/path/to/platform-home --json
knowledge-platform plan backup --home /absolute/path/to/platform-home --json
knowledge-platform plan upgrade --home /absolute/path/to/platform-home --json
knowledge-platform infrastructure --home /absolute/path/to/platform-home --apply --json
knowledge-platform backup --home /absolute/path/to/platform-home --output /absolute/path/to/snapshot --apply --json
knowledge-platform backup --validate --output /absolute/path/to/snapshot --json
# Build a local-only PREPARED migration manifest from that validated snapshot:
PYTHONPATH=backend backend/.venv/bin/python \
  backend/scripts/phase10_local_migration_manifest.py \
  --backup-dir /absolute/path/to/snapshot \
  --stage-report /absolute/path/to/local-catalog-stage-report.json \
  --output /absolute/path/to/migration.json
knowledge-platform upgrade --home /absolute/path/to/platform-home --backup /absolute/path/to/snapshot --migration-manifest /absolute/path/to/migration.json --apply --json
knowledge-platform restore --backup /absolute/path/to/snapshot --target /absolute/path/to/restored-home --json
knowledge-platform restore --backup /absolute/path/to/snapshot --target /absolute/path/to/restored-home --apply --json
knowledge-platform deploy --home /absolute/path/to/platform-home --runtime-bundle /path/to/bundle --apply --json
knowledge-platform index --home /absolute/path/to/platform-home --apply --json

# Only after operator review of the rendered Compose config:
PUDDINGKNOWLEDGE_HOME=/absolute/path/to/platform-home \
  packages/knowledge-platform-deploy-cli/assets/platform-infra.sh plan
```

`init` creates only an isolated Platform Home, including Platform-owned
persistent infrastructure directories and non-secret control-plane metadata.
The other commands have a dry plan by default. `infrastructure --apply` records
the Platform service set as a staged operation, but this local slice deliberately
does not invoke Docker, start a process, change the legacy Catalog, migrate
files, or activate a revision. Runtime execution, package import/export and
index building will attach to their Platform workers in later slices.

The shipped `assets/platform-infra.sh` is the separate supervisor boundary for
an operator-approved Compose run. Its `plan` validates the rendered Platform
Compose config; `up`, `down`, and `status` are explicit Docker operations and
are not called by the CLI staging command.

Runtime bundle staging is content-addressed and fail-closed: every regular file
under the bundle (apart from `manifest.json`) must be declared in
`manifest.json` with its SHA-256 digest. Empty manifests, wildcard paths,
undeclared files, and symlinks are rejected before any Platform Home state is
written. An applied bundle is copied into the Platform-owned
`runtime/releases/<manifest-digest>/` directory through a temporary sibling and
atomic rename; `deployment.json` records that owned path. The release is
verified again after copying, and an existing digest directory is reused only
when its complete manifest and file contents match. A failed copy never
publishes a deployment, and this command still does not start any process.

`health` is a read-only observation of Platform Home metadata and deliberately
does not probe Docker, external providers, or production endpoints. `backup`
now creates a local, file-level Platform snapshot when `--output` is supplied;
the output contains a versioned manifest and SHA-256 checksums for
`platform.json`, `deployment.json`, `catalog/`, `packages/`, `runtime/`, and
`infrastructure/`. `logs/` and `operations/` are explicitly excluded. Symlinks,
special files, source changes during read, and secret-bearing markers are
rejected before the target directory is published. `backup --validate` checks
the published snapshot independently. `upgrade` still exposes a staged
operation only; it requires a separately validated backup snapshot and records
its manifest digest with an open rollback window marker. It does not change a
revision or close a rollback window until the independent migration runner is
attached. The staging request also requires a prepared Phase 10 Installation
Migration Manifest whose snapshot digest matches the backup and whose rollback
window is open.

`restore` validates the snapshot first and, with `--apply`, restores only into
a new local target directory using staging plus rename. It refuses to overwrite
an existing target and never deletes the source snapshot.

## Executable local runtime

The local Catalog/Wiki runtime can now be installed and supervised independently.
Build its content-addressed bundle from the independent Knowledge checkout:

```bash
uv run --project backend --no-sync python packages/knowledge-platform-runtime/stage.py --output /absolute/new/runtime-bundle
node packages/knowledge-platform-deploy-cli/src/cli.mjs init --home /absolute/platform-home --json
node packages/knowledge-platform-deploy-cli/src/cli.mjs deploy --home /absolute/platform-home --runtime-bundle /absolute/new/runtime-bundle --apply --json
node packages/knowledge-platform-deploy-cli/src/cli.mjs install --home /absolute/platform-home --apply --json
node packages/knowledge-platform-deploy-cli/src/cli.mjs start --home /absolute/platform-home --catalog /absolute/catalog.sqlite3 --wiki-root /absolute/wiki --port 18989 --apply --json
node packages/knowledge-platform-deploy-cli/src/cli.mjs status --home /absolute/platform-home --json
node packages/knowledge-platform-deploy-cli/src/cli.mjs health --home /absolute/platform-home --json
node packages/knowledge-platform-deploy-cli/src/cli.mjs stop --home /absolute/platform-home --apply --json
```

Installation requires `uv` on PATH. It installs locked dependencies and a
non-editable wheel into `Home/environments/<digest>`, using a disposable build
copy. The release tree remains unchanged. An installed import probe must pass
before `runtime/installed.json` is published; a failed attempt removes only its
own new environment. Repeating a completed installation probes and reuses it.
The original bundle directory is no longer required after deployment.

`start` accepts explicit Catalog/Wiki inputs and optional `--database-config`
and `--structured-config`; it uses a new local snapshot on each run. It does not
activate production or make authoring state durable across snapshots. The
supervisor owns its child, authenticates local control messages, and checks the
real endpoint before reporting `running`. Control files and temporary snapshots
live under `Home/run`; they and rebuildable `Home/environments` are excluded from
the existing backup roots. `health` exits nonzero when an installed runtime is
not running. These commands do not launch Docker or complete the staged
production migration/upgrade commands described above.

Replay the complete local installation and lifecycle with generated data:

```bash
python3 integration/local_service_smoke.py --repo /absolute/PuddingKnowledge --fixture-python /absolute/PuddingKnowledge/backend/.venv/bin/python
```
