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
