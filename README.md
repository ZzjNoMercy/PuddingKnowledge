# PuddingKnowledge

Independent development checkout extracted from PuddingClaw history.

Backend: `cd backend && uv sync --locked && uv run puddingknowledge-local --help`.
Product UI: `cd packages/knowledge-platform-web && npm ci && npm run build`.
Other packages own their individual package manifests and tests.

The backend owns knowledge_platform and knowledge_contracts; no legacy Knowledge/Analytics/Vanna implementation is included. The local runtime uses explicit configuration and fixture-safe development identities. Production authentication, complete distribution and stateful upgrade/rollback validation remain incomplete.

See docs/knowledge-platform/repository-separation-workplan.md for the remaining work.
