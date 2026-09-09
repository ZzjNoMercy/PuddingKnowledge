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
written.

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
