# PuddingKnowledge Web

Independent product frontend for the Knowledge Platform. It reuses the visual
and interaction model of PuddingClaw's Knowledge workspace while replacing the
Claw shell, global session store, and legacy `/api` client with the public
Platform `/v1` contract.

## Local development

```bash
npm install
PLATFORM_API_URL=http://127.0.0.1:8889 npm run dev -- --port 8090
```

For normal testing, use the repository launcher instead:

```bash
./scripts/start-knowledge-local.sh --open
```

The product UI is served at `/knowledge`. The sibling
`knowledge-platform-console` package is a contract diagnostics surface and is
not the product frontend.

## Boundary

- no imports from `frontend/`, Claw state, Session, Chat, Task, or Electron;
- browser requests use same-origin `/v1` and `/mcp` paths;
- a runtime same-origin BFF forwards those paths only to an explicit loopback `PLATFORM_API_URL`;
- no host file paths, credentials, or raw SQL are accepted by this UI.

## Wiki authoring

`/knowledge/schema` provides registered Raw selection, existing-page selection,
model proposal generation, Markdown/index/log review, preview and explicit apply.
The API must be started with `--wiki-authoring`; generation also requires an
explicit `--wiki-authoring-model-config`. Queue execution requires the separately
opted-in `--wiki-authoring-worker`. A ready proposal never publishes automatically.
The UI can enqueue, inspect proposals, cancel or reschedule queued work, and
explicitly abandon unsettled proposals with a reason and current receipt.

Mutations retain their exact request and operation ID in this tab's Space-scoped
session storage before dispatch. Failed or missing responses leave a recovery
button, including after reload. Storage failure prevents dispatch; a corrupt
record has an explicit local cleanup action. Cleanup does not cancel server work.
The last proposal ID can be reloaded with “读取提案”; closing the tab may discard
local recovery state. Edited drafts are not automatically saved. A fresh preview
is required after edits and the server still checks revision CAS at apply time.

The BFF measures actual streamed bytes: fixed authoring actions accept up to
32 MiB; other requests retain the 1 MiB limit. Raw discovery is paginated and
contains registered snapshot paths and hashes, not host filesystem metadata.
These local product controls do not establish installation cutover, rollback,
production writer authority or complete repository separation.
