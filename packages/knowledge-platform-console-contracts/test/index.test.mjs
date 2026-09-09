import test from "node:test";
import assert from "node:assert/strict";
import {
  createPlatformClient,
  parseMcpResourceList,
  parseMcpResourceRead,
  parseQueryResult,
  PlatformApiError,
} from "../src/index.mjs";

function okResult() {
  return {
    status: "ok",
    trace_id: "trace-1",
    data: { spaces: [] },
    evidence: [{
      asset_id: "asset-1",
      resource_uri: "knowledge://spaces/space-1/assets/asset-1",
      locator: { section: "intro" },
    }],
  };
}

test("Console client uses public REST paths and validates the result envelope", async () => {
  const calls = [];
  const client = createPlatformClient({
    baseUrl: "http://127.0.0.1:8080",
    headers: { authorization: "Bearer test" },
    fetchImpl: async (url, options) => {
      calls.push({ url, options });
      return new Response(JSON.stringify(okResult()), { status: 200, headers: { "content-type": "application/json" } });
    },
  });
  const result = await client.listDatasets({ spaceId: "space-1" });
  assert.equal(result.status, "ok");
  assert.equal(calls[0].url, "http://127.0.0.1:8080/v1/datasets?space_id=space-1");
  assert.equal(calls[0].options.body, undefined);
  assert.equal(calls[0].options.headers.authorization, "Bearer test");
});

test("Console client uses Collection as the canonical top-level resource", async () => {
  const calls = [];
  const client = createPlatformClient({
    baseUrl: "http://127.0.0.1:8080",
    fetchImpl: async (url, options) => {
      calls.push({ url, options });
      return new Response(JSON.stringify(okResult()), { status: 200 });
    },
  });
  await client.listCollections({ spaceId: "space-1" });
  assert.equal(calls[0].url, "http://127.0.0.1:8080/v1/collections?space_id=space-1");
});

test("Console client sends only JSON bodies for query operations", async () => {
  const calls = [];
  const client = createPlatformClient({
    baseUrl: "/platform",
    fetchImpl: async (url, options) => {
      calls.push({ url, options });
      return new Response(JSON.stringify(okResult()), { status: 200 });
    },
  });
  await client.knowledgeQuery({ query: "hello", space_id: "space-1" });
  assert.equal(calls[0].url, "/platform/v1/knowledge/query");
  assert.equal(calls[0].options.headers["content-type"], "application/json");
  assert.deepEqual(JSON.parse(calls[0].options.body), { query: "hello", space_id: "space-1" });
});

test("Console client exposes the generic Query Plane alias", async () => {
  const calls = [];
  const client = createPlatformClient({
    baseUrl: "http://127.0.0.1:8080",
    fetchImpl: async (url, options) => {
      calls.push({ url, options });
      return new Response(JSON.stringify(okResult()), { status: 200 });
    },
  });
  await client.query({ query: "hello", space_id: "space-1" });
  assert.equal(calls[0].url, "http://127.0.0.1:8080/v1/query");
});

test("Console client reads QueryResult metadata through the fixed route", async () => {
  const calls = [];
  const client = createPlatformClient({
    baseUrl: "http://127.0.0.1:8080",
    fetchImpl: async (url, options) => {
      calls.push({ url, options });
      return new Response(JSON.stringify({ status: "ok", data: { query_result: { id: "query_result_1" } } }), { status: 200 });
    },
  });
  const result = await client.getQueryResult("query_result_1");
  assert.equal(result.data.query_result.id, "query_result_1");
  assert.equal(calls[0].url, "http://127.0.0.1:8080/v1/query-results/query_result_1");
  assert.equal(calls[0].options.body, undefined);
});

test("Console client keeps Analytics Database Query as a two-phase fixed route", async () => {
  const calls = [];
  const client = createPlatformClient({
    baseUrl: "http://127.0.0.1:8080",
    fetchImpl: async (url, options) => {
      calls.push({ url, options });
      const isPlan = url.endsWith("/v1/database/nl2sql");
      return new Response(JSON.stringify({
        status: "ok",
        data: isPlan
          ? { query_plan: { query_plan_id: "qp_1", dataset_id: "dataset-1", dialect: "postgres" }, sql_hash: "sha256:" + "a".repeat(64) }
          : { query_plan_id: "qp_1", columns: ["count"], rows: [{ count: 1 }], row_count: 1 },
      }), { status: 200 });
    },
  });
  const plan = await client.databaseNl2Sql({ space_id: "space-1", dataset_id: "dataset-1", question: "按车型统计数量" });
  assert.equal(plan.data.query_plan.query_plan_id, "qp_1");
  await client.executeDatabasePlan("qp_1", {
    space_id: "space-1",
    page_size: 100,
    expected_sql_hash: "sha256:" + "a".repeat(64),
  });
  assert.equal(calls[0].url, "http://127.0.0.1:8080/v1/database/nl2sql");
  assert.deepEqual(JSON.parse(calls[0].options.body), { space_id: "space-1", dataset_id: "dataset-1", question: "按车型统计数量" });
  assert.equal(calls[1].url, "http://127.0.0.1:8080/v1/database/query-plans/qp_1:execute");
  assert.equal(JSON.parse(calls[1].options.body).expected_sql_hash, "sha256:" + "a".repeat(64));
  assert.equal(JSON.stringify(calls).includes("sql:"), false);
});

test("Console client reads only the binding-scoped Database Schema route", async () => {
  const calls = [];
  const client = createPlatformClient({
    baseUrl: "http://127.0.0.1:8080",
    fetchImpl: async (url, options) => {
      calls.push({ url, options });
      return new Response(JSON.stringify({
        status: "ok",
        data: {
          space_id: "space-1",
          dataset_id: "dataset-1",
          source_revision: "sha256:" + "a".repeat(64),
          tables: [{ table_name: "public.sales", columns: ["id", "amount"], schema_revision: "sha256:" + "b".repeat(64) }],
        },
      }), { status: 200 });
    },
  });
  const result = await client.listDatabaseSchema({ spaceId: "space-1", datasetId: "dataset-1" });
  assert.equal(result.data.tables[0].table_name, "public.sales");
  assert.equal(calls[0].url, "http://127.0.0.1:8080/v1/database/schema?space_id=space-1&dataset_id=dataset-1");
  assert.equal(calls[0].options.body, undefined);
  assert.equal(JSON.stringify(calls).includes("connection"), false);
});

test("Console client reads Admin job metadata through the fixed route", async () => {
  const calls = [];
  const client = createPlatformClient({
    baseUrl: "http://127.0.0.1:8080",
    fetchImpl: async (url, options) => {
      calls.push({ url, options });
      return new Response(JSON.stringify({ status: "ok", data: { job: { id: "processing_job_1" } } }), { status: 200 });
    },
  });
  const result = await client.getJob("processing_job_1");
  assert.equal(result.data.job.id, "processing_job_1");
  assert.equal(calls[0].url, "http://127.0.0.1:8080/v1/jobs/processing_job_1");
  assert.equal(calls[0].options.body, undefined);
});

test("Console client discovers Semantic Assets through the fixed Admin route", async () => {
  const calls = [];
  const client = createPlatformClient({
    baseUrl: "http://127.0.0.1:8080",
    fetchImpl: async (url, options) => {
      calls.push({ url, options });
      return new Response(JSON.stringify({ status: "ok", data: { assets: [{ id: "dimension:brand", status: "active" }] } }), { status: 200 });
    },
  });
  const result = await client.listSemanticAssets({ spaceId: "space-1", status: "active" });
  assert.equal(result.data.assets[0].id, "dimension:brand");
  assert.equal(calls[0].url, "http://127.0.0.1:8080/v1/semantic-assets?space_id=space-1&status=active");
  assert.equal(calls[0].options.body, undefined);
});

test("Console client discovers Connectors and Source Items through fixed Admin routes", async () => {
  const calls = [];
  const client = createPlatformClient({
    baseUrl: "http://127.0.0.1:8080",
    fetchImpl: async (url, options) => {
      calls.push({ url, options });
      return new Response(JSON.stringify({ status: "ok", data: { connectors: [], source_items: [] } }), { status: 200 });
    },
  });
  await client.listConnectors({ spaceId: "space-1" });
  await client.listSourceItems({ spaceId: "space-1", connectorId: "connector-1" });
  assert.equal(calls[0].url, "http://127.0.0.1:8080/v1/connectors?space_id=space-1");
  assert.equal(calls[1].url, "http://127.0.0.1:8080/v1/source-items?space_id=space-1&connector_id=connector-1");
});

test("Console client exposes host-managed Connector authorization without secret fields", async () => {
  const calls = [];
  const client = createPlatformClient({
    baseUrl: "http://127.0.0.1:8080",
    fetchImpl: async (url, options) => {
      calls.push({ url, options });
      return new Response(JSON.stringify({
        status: "ok",
        data: { authorization: { status: "awaiting_host_authorization" }, authorizations: [] },
      }), { status: 200 });
    },
  });
  await client.listConnectorAuthorizations({ spaceId: "space-1" });
  await client.authorizeConnector("connector-1", {
    space_id: "space-1",
    mode: "user_reauthorize",
    idempotency_key: "auth-1",
  });
  assert.equal(calls[0].url, "http://127.0.0.1:8080/v1/connector-authorizations?space_id=space-1");
  assert.equal(calls[1].url, "http://127.0.0.1:8080/v1/connectors/connector-1:authorize");
  assert.equal(JSON.parse(calls[1].options.body).idempotency_key, "auth-1");
  assert.equal(JSON.stringify(calls).includes("token"), false);
});

test("Console client reads only the Space-scoped Platform notification route", async () => {
  const calls = [];
  const client = createPlatformClient({
    baseUrl: "http://127.0.0.1:8080",
    fetchImpl: async (url, options) => {
      calls.push({ url, options });
      return new Response(JSON.stringify({ status: "ok", data: { notifications: [], count: 0 } }), { status: 200 });
    },
  });
  await client.listNotifications({ spaceId: "space-1", limit: 10 });
  assert.equal(calls[0].url, "http://127.0.0.1:8080/v1/notifications?space_id=space-1&limit=10");
  assert.equal(calls[0].options.body, undefined);
});

test("Console client reads the path-free Asset binding review queue route", async () => {
  const calls = [];
  const client = createPlatformClient({
    baseUrl: "http://127.0.0.1:8080",
    fetchImpl: async (url, options) => {
      calls.push({ url, options });
      return new Response(JSON.stringify({
        status: "ok",
        data: { asset_binding_review_queue: { execution_allowed: false, items: [] } },
      }), { status: 200 });
    },
  });
  const result = await client.listAssetBindingReviews({ spaceId: "space-1" });
  assert.equal(result.data.asset_binding_review_queue.execution_allowed, false);
  assert.equal(calls[0].url, "http://127.0.0.1:8080/v1/asset-binding-reviews?space_id=space-1");
  assert.equal(calls[0].options.body, undefined);
});

test("Console client uses fixed Upload and Package Import Admin routes", async () => {
  const calls = [];
  const client = createPlatformClient({
    baseUrl: "http://127.0.0.1:8080",
    fetchImpl: async (url, options) => {
      calls.push({ url, options });
      return new Response(JSON.stringify({ status: "ok", data: { staged: true } }), { status: 200 });
    },
  });
  await client.uploadAsset({
    asset_id: "asset-1",
    space_id: "space-1",
    title: "Notes",
    filename: "notes.md",
    mime_type: "text/markdown",
    binding_id: "binding-1",
    content_digest: "sha256:" + "a".repeat(64),
    idempotency_key: "upload-1",
  });
  await client.importPackage({ package_ref: "package-1", idempotency_key: "import-1" });
  await client.rebuildIndex({
    space_id: "space-1",
    collection_id: "collection-1",
    collection_version: "v1",
    capability: "document_rag_query",
    provider_id: "provider-1",
    idempotency_key: "index-1",
  });
  assert.equal(calls[0].url, "http://127.0.0.1:8080/v1/assets:upload");
  assert.equal(calls[1].url, "http://127.0.0.1:8080/v1/packages:import");
  assert.equal(calls[2].url, "http://127.0.0.1:8080/v1/indexes:rebuild");
  assert.equal(calls[0].options.headers["content-type"], "application/json");
  assert.equal(JSON.parse(calls[0].options.body).binding_id, "binding-1");
  await assert.rejects(client.uploadAsset({ source_path: "/Users/pet/private.md" }), /not portable/);
});

test("Console client uses fixed derivative routes and bounded range query", async () => {
  const calls = [];
  const client = createPlatformClient({
    baseUrl: "http://127.0.0.1:8080",
    fetchImpl: async (url, options) => {
      calls.push({ url, options });
      return new Response(JSON.stringify({ status: "ok", data: { derivatives: [] } }), { status: 200 });
    },
  });
  await client.listAssetDerivatives("asset-1");
  await client.readAssetDerivative("asset-1", "normalized_markdown", { start: 4, end: 16 });
  assert.equal(calls[0].url, "http://127.0.0.1:8080/v1/assets/asset-1/derivatives");
  assert.equal(calls[1].url, "http://127.0.0.1:8080/v1/assets/asset-1/derivatives/normalized_markdown?start=4&end=16");
  assert.equal(calls[1].options.body, undefined);
});

test("Console client discovers and reads MCP Resources through the fixed JSON-RPC endpoint", async () => {
  const calls = [];
  const client = createPlatformClient({
    baseUrl: "http://127.0.0.1:8080",
    fetchImpl: async (url, options) => {
      calls.push({ url, options });
      const request = JSON.parse(options.body);
      const result = request.method === "resources/list"
        ? {
          resources: [{ uri: "knowledge://spaces/space-1/manifest", name: "Space" }],
          resourceTemplates: [{ uriTemplate: "knowledge://spaces/{space_id}/assets/{asset_id}" }],
        }
        : {
          contents: [{ uri: "knowledge://spaces/space-1/manifest", mimeType: "application/json", text: '{"id":"space-1"}' }],
        };
      return new Response(JSON.stringify({ jsonrpc: "2.0", id: request.id, result }), { status: 200 });
    },
  });
  const listed = await client.listMcpResources();
  const read = await client.readMcpResource("knowledge://spaces/space-1/manifest", { start: 0, end: 64 });
  assert.equal(listed.resources[0].uri, "knowledge://spaces/space-1/manifest");
  assert.equal(read.contents[0].text, '{"id":"space-1"}');
  assert.equal(calls[0].url, "http://127.0.0.1:8080/mcp");
  assert.equal(JSON.parse(calls[1].options.body).params.end, 64);
});

test("MCP Resource client rejects non-portable descriptors and content", () => {
  assert.throws(
    () => parseMcpResourceList({ resources: [], resourceTemplates: [{ uriTemplate: "https://example.com/{id}" }] }),
    /portable/,
  );
  assert.throws(
    () => parseMcpResourceRead({ contents: [{ uri: "knowledge://spaces/space-1/manifest", text: "path=/Users/pet/private" }] }),
    /portable/,
  );
  assert.throws(
    () => parseMcpResourceRead({ contents: [{ uri: "knowledge://spaces/space-1/manifest", text: String.raw`source=C:\Users\pet\private.txt` }] }),
    /portable/,
  );
  assert.throws(
    () => parseMcpResourceRead({ contents: [{ uri: "knowledge://spaces/space-1/manifest", text: "prefix=/srv/private.txt" }] }),
    /portable/,
  );
  assert.throws(
    () => parseMcpResourceRead({ contents: [{ uri: "knowledge://spaces/space-1/manifest", text: "unsafe\u0000text" }] }),
    /portable/,
  );
  assert.throws(
    () => parseMcpResourceRead({
      contents: [{ uri: "knowledge://spaces/space-1/manifest", text: "ok" }],
      structuredContent: { artifact_path: "/private/secret" },
    }),
    /non-portable/,
  );
});

test("Console client rejects non-portable Evidence and does not expose response bodies in errors", async () => {
  assert.doesNotThrow(() => parseQueryResult({
    status: "ok",
    evidence: [{
      asset_id: "connector-1",
      resource_uri: "knowledge://spaces/space-1/connectors/connector-1/authorization",
      locator: { section: "host_managed_authorization" },
      quote: "",
      revision: "",
      score: null,
      matched_by: ["host_managed"],
    }],
  }));
  assert.throws(
    () => parseQueryResult({ status: "ok", evidence: [{ asset_id: "asset-1", resource_uri: "/Users/pet/secret.md" }] }),
    /portable/,
  );
  assert.throws(
    () => parseQueryResult({
      status: "ok",
      evidence: [{
        asset_id: "asset-1",
        resource_uri: "knowledge://spaces/space-1/assets/asset-1",
        locator: { section: String.raw`C:\Users\pet\secret.md` },
      }],
    }),
    /portable/,
  );
  assert.throws(
    () => parseQueryResult({
      status: "ok",
      evidence: [{
        asset_id: "asset-1",
        resource_uri: "knowledge://spaces/space-1/assets/asset-1",
        quote: "unsafe\u0000text",
        score: Number.NaN,
      }],
    }),
    /portable|invalid/,
  );
  assert.throws(
    () => parseQueryResult({
      status: "ok",
      evidence: [{
        asset_id: "asset-1",
        resource_uri: "knowledge://spaces/space-1/assets/asset-1",
        revision: "sha256:bad",
      }],
    }),
    /invalid/,
  );
  assert.throws(
    () => parseQueryResult({ status: "ok", answer: String.raw`source=C:\Users\pet\secret.md` }),
    /portable/,
  );
  assert.throws(
    () => parseQueryResult({ status: "ok", data: { artifact_path: "/private/secret" } }),
    /non-portable/,
  );
  assert.throws(
    () => parseQueryResult({ status: "error", error: { code: "internal_error", message: "/Users/pet/secret" } }),
    /portable/,
  );
  const client = createPlatformClient({
    fetchImpl: async () => new Response(JSON.stringify({ error: { code: "permission_denied", message: "/Users/pet/secret" } }), { status: 403 }),
  });
  await assert.rejects(client.listSpaces(), (error) => {
    assert.ok(error instanceof PlatformApiError);
    assert.equal(error.status, 403);
    assert.equal(error.code, "permission_denied");
    assert.equal(error.message.includes("/Users/pet"), false);
    return true;
  });
});

test("Console Admin client exposes fixed Platform routes and rejects host file fields", async () => {
  const calls = [];
  const client = createPlatformClient({
    baseUrl: "http://127.0.0.1:8080",
    fetchImpl: async (url, options) => {
      calls.push({ url, options });
      return new Response(JSON.stringify({ status: "ok", data: {} }), { status: 200 });
    },
  });
  await client.decideSemanticDimension("job-1", { space_id: "space-1", decision: "confirm" });
  assert.equal(calls[0].url, "http://127.0.0.1:8080/v1/semantic-dimensions/jobs/job-1:decision");
  await assert.rejects(
    client.compileWiki("asset-1", { source_path: "/Users/pet/private.md" }),
    /not portable/,
  );
  await assert.rejects(
    client.prepareSemanticAsset({ sources: [{ file_path: "/Users/pet/private.md" }] }),
    /not portable/,
  );
});
