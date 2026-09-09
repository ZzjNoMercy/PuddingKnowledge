# Knowledge Platform Console Contracts

This package is the dependency-free client boundary for the independent
Platform Console. It speaks only the public `/v1` REST contract and uses
portable `knowledge://` resource URIs. It does not import PuddingClaw,
FastAPI, React, or any legacy Knowledge/Analytics module.

The package is intentionally small during the staged migration. It provides
bounded Catalog/Asset reads, the generic and routed query operations needed by
a Console shell, and fixed Admin method names. The Console currently exposes
only the first freshness Admin workflow; other Admin/Job screens require their
Platform contracts and local process evidence before being added.

```js
import { createPlatformClient } from "@puddingai/knowledge-platform-console-contracts";

const client = createPlatformClient({ baseUrl: "http://127.0.0.1:8080" });
const spaces = await client.listSpaces();
const assets = await client.listAssets({ spaceId: "space_demo" });
const result = await client.knowledgeQuery({ query: "查找相关知识", space_id: "space_demo" });
```

Upload and Package Import methods accept only logical metadata plus an opaque
host binding ID; the host keeps the actual local file path and the Platform
returns staging metadata rather than activation state. The package owns no
credentials, filesystem paths, catalog state, or service processes. The host
supplies `fetch` and, when needed, its authenticated request headers.
