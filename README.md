# PuddingKnowledge

Independent development checkout extracted from PuddingClaw history.

Backend: `cd backend && uv sync --locked --no-editable && uv run --no-sync puddingknowledge-local --help`.
Tests: from backend, run `uv run --no-sync python -m pytest -q`. Optional PostgreSQL and Excel coverage uses `uv sync --locked --no-editable --all-extras`; real PostgreSQL replay also requires local initdb/postgres/psql binaries. The tests create isolated temporary databases and do not use an existing server. Before full all-extras process replay, run the Product UI install/build below: the launcher integration intentionally reuses that production build with --no-build.
Product UI: `cd packages/knowledge-platform-web && npm ci && npm run build`.
Other packages own their individual package manifests and tests.

The backend pyproject.toml and uv.lock are the authoritative install metadata. After Python changes, rebuild the installed package with `uv sync --locked --no-editable --reinstall-package puddingknowledge-local`. The runtime staging command copies this same metadata and only Platform-owned modules; it does not depend on a PuddingClaw or Harness checkout.

Legacy source-observer tests live under backend/tests/migration and are excluded from default traversal. Running them requires an explicit PUDDINGKNOWLEDGE_LEGACY_SOURCE fixture; they cannot substitute for independent Platform tests. See that directory's README for the original-source evidence boundary.

integration/mcp_process_smoke.py verifies two separately installed processes using generated Catalog/Wiki fixtures. Pass --knowledge-python and --harness-python explicitly. It verifies resource reads, disabled server/wrong Space/unknown asset denials, disconnection handling, original Catalog preservation and absence of legacy Home writes.

The backend owns knowledge_platform and knowledge_contracts; no legacy Knowledge/Analytics/Vanna implementation is included. The local runtime uses explicit configuration and fixture-safe development identities. Production authentication, complete distribution and stateful upgrade/rollback validation remain incomplete.

See docs/knowledge-platform/repository-separation-workplan.md for the remaining work.

Persistent local authoring is available with `--state-dir /absolute/owned-state`.
The first start copies the explicit Catalog and Wiki seed into that directory;
subsequent starts reuse its Catalog, so published datasets and Wiki survive restarts.
Wiki compilation additionally accepts `--wiki-config /absolute/wiki.json` with
an explicit HTTP model endpoint and approved source bindings. See
[the persistent runtime guide](docs/knowledge-platform/persistent-local-runtime.md).

The persistent runtime also accepts `--capture-config` for public URL ingestion,
owned Raw Snapshots, retry and Wiki promotion. Source URLs stay in its encrypted
Vault. See the same persistent runtime guide for API and policy details.
