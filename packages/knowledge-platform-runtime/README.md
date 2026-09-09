# Knowledge local runtime

The launcher now uses `python -m knowledge_platform.local`. Its Catalog snapshot,
Wiki materialization and REST/MCP composition live inside `knowledge_platform`;
there is no runtime import from the repository's rehearsal `scripts` package.

This package validates the current local Catalog/Wiki/PostgreSQL product slice. It is not
a production server or the complete Platform distribution. It binds IPv4
loopback with a fixed local principal and never activates a deployment. Additional provider, connector and authoring rehearsal modes remain in their
existing tools.

## Independent installation

From the repository root, choose a new staging path:

```bash
python3 packages/knowledge-platform-runtime/stage.py --output /private/tmp/knowledge-install
uv sync --project /private/tmp/knowledge-install --locked --no-editable
/private/tmp/knowledge-install/.venv/bin/puddingknowledge-local --help
```

The generated tree contains only `knowledge_platform`, `knowledge_contracts`,
package metadata and its own `uv.lock`. It needs neither the Claw virtualenv nor
`PYTHONPATH`. Network is needed only if locked dependencies are missing locally.
The lock covers this local query surface, not every optional Platform provider.

```bash
/private/tmp/knowledge-install/.venv/bin/puddingknowledge-local \
  --catalog /absolute/path/to/staged-catalog.sqlite3 \
  --wiki-root /absolute/path/to/published-wiki \
  --temp-dir /private/tmp/knowledge-install/run \
  --ready-file /private/tmp/knowledge-install/ready.json \
  --port 8895
```

Both output paths must be new. The Catalog is snapshotted with SQLite backup;
Wiki bindings point to the explicit local root. Readiness is an initialization
signal; consumers must also verify `/v1/spaces`. SIGINT/SIGTERM remove that signal.
The private run directory remains available for inspection after shutdown.

## Verification

Set `KNOWLEDGE_TEST_PYTHON` to the independent environment's Python when running
`backend/tests/test_knowledge_platform_local_runtime.py`. Its child process runs
outside the repository without `PYTHONPATH`, exercises discovery/search/read,
occupied-port rejection, reused-workspace rejection, Catalog digest preservation,
and termination cleanup.

## PostgreSQL and local Vanna examples

Install the optional adapter in the independent tree:

```bash
uv sync --project /private/tmp/knowledge-install --locked --no-editable --extra postgres
```

Copy `database.example.json` to a host-local file and supply your explicit local
source, table allowlist and the three verified Vanna identities. The example's
placeholder digests intentionally fail validation. Configuration is limited to
`space_kb_default`, matching this local Catalog/Wiki slice. The target Collection
must already exist; its current version is read from the private Catalog copy.

Pass `--database-config /absolute/path/to/database.json` to either the standalone
CLI or `./scripts/start-knowledge-local.sh`. The repository launcher requires
`asyncpg` in its backend environment; the independent package installs it via the
`postgres` extra. `--check` validates configuration/dependency availability without
connecting to PostgreSQL. Actual startup verifies the Collection and database.

The password is read only from the explicitly named `KNOWLEDGE_DB_*` environment
variable, or `password_env: null` for an explicitly passwordless local source.
Never put a literal password in JSON. The launcher passes only that variable to
the API child, not to the product Web process. Configuration paths and credentials
remain host-local.

The same server now supports Catalog/Wiki and database schema, plan generation
and read-only execution through REST/MCP. The local Vanna gateway matches existing
SQL examples; it does not call an LLM or generate answers for unseen questions.
No database is configured by default. Plans remain server-owned and ephemeral;
execution rechecks SQL hash, scope and source revision. Only the private Catalog
copy receives the database binding, and activation remains disabled.

## Explicit local tables and logical dataset processing

The independent local runtime can also compose the owned Table Query,
LogicalDatasetAuthoringService and durable LogicalDatasetProcessingWorker.
Supply `--structured-config /absolute/path/to/structured.json`:

```json
{
  "version": 1,
  "space_id": "space_kb_default",
  "assets": {
    "existing-source-id": "/absolute/path/to/source.csv"
  }
}
```

Each source must already be approved in the input Catalog and its bytes must
match that Catalog's digest. Paths are host-local configuration and are never
accepted from HTTP request bodies. This option grants the fixed local principal
Table Query, Admin and Processing scopes within the local composition; do not
expose this loopback-only server as a multi-user or production service.

The composed REST surface supports `POST /v1/datasets`,
`POST /v1/datasets/{id}:publish` (with a stable `idempotency_key`), and
`POST /v1/table/query`. Published logical definitions are resolved dynamically
against the configured source allowlist, so new datasets can be queried without
restarting the service. Durable jobs support idempotent replay after rebuilding
the service. No source CSV/TSV file is modified.

The CLI still creates a private Catalog snapshot for each run. Authoring changes
live in `<temp-dir>/knowledge-platform.sqlite3`; retain that file and explicitly
use it as the input Catalog when carrying state into another CLI run. Reusing the
original input Catalog starts from the original state. This is not yet the full
persistent production-server lifecycle or a complete Processing/Authoring bundle.

`backend/tests/test_knowledge_platform_local_structured.py` covers real SQLite
creation/publication/query/replay and runs the standalone CLI outside the checkout
when `KNOWLEDGE_TEST_PYTHON` points to the independent installation. The source
Catalog and source table bytes remain unchanged.


Continuous restart is covered with two separate installed CLI processes on the
same loopback port, passing the first owned Catalog into the second. Local Wiki
materialization updates its own existing page records without duplicate Collection
entries; an ID owned by another source is rejected. An active listener still
blocks startup before creating output. Sources must use canonical absolute paths
on macOS (for example `/private/tmp`, not the `/tmp` symlink); arbitrary symlink
components remain rejected.
