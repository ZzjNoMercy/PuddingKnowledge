# Knowledge Platform Contract Diagnostics Console

> This package is a contract and boundary diagnostics surface. The user-facing
> product UI lives in `packages/knowledge-platform-web`.

An independent static Console shell for the Platform public REST contract.
It has no PuddingClaw, React, FastAPI, or legacy Knowledge/Analytics runtime
dependency. The build copies the versioned Console contract client into the
static output so the result can be served without the monorepo checkout.

From the repository root, the local-only API and Console can be started together:

```bash
./scripts/start-knowledge-local.sh
```

The launcher uses the staged local Catalog, discovers the existing local Wiki,
binds both processes to loopback, waits for readiness, and owns only the child
processes it starts. Occupied default ports advance to the next free loopback
port without terminating the existing process. Use `Ctrl+C` to stop them. Run
`./scripts/start-knowledge-local.sh --help` for explicit path and port overrides.

```bash
npm run build
# serve dist/ behind the Platform API origin or a same-origin reverse proxy

# optional: exercise the current local PostgreSQL binding through a real
# independent Platform process (never a production endpoint)
npm run test:database
```

The current slice provides Space/Collection/Asset discovery, bounded Asset reads,
the generic `query` and `knowledge_query` views, portable Evidence rendering,
Collection freshness, the binding-scoped Database Schema and two-phase
read-only Analytics Database Query views,
Connector/SourceItem discovery, host-managed OAuth status and authorization
intents, and host-binding Upload / Package Import staging.
Index activation and other Admin workflows remain separate until their
Platform contracts and local process evidence are complete. Notifications are
read only through an explicit Space-scoped Platform event binding; inbox/read
acknowledgement remains owned by the consuming product.

The generated `dist/manifest.json` also declares the eight release surfaces
(`knowledge`, `analytics`, `oauth`, `imports`, `sources`, `schema`, `results`,
and `notifications`) and their public contract boundary. Every surface remains
`activation_allowed: false` in this local shadow build.
