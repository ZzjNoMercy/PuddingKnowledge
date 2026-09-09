import test from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { spawn } from "node:child_process";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createPlatformClient, parseQueryResult } from "../../knowledge-platform-console-contracts/src/index.mjs";

const packageDir = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(packageDir, "../../..");
const catalog = path.join(repoRoot, "artifacts/phase0b-local-catalog/knowledge-platform.sqlite3");
const wikiRoot = path.join(os.homedir(), "Documents/knowledge/imported/20260804");
const python = path.join(repoRoot, "backend/.venv/bin/python");

function digest(bytes) {
  return `sha256:${createHash("sha256").update(bytes).digest("hex")}`;
}

async function freePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      const port = address && typeof address === "object" ? address.port : null;
      server.close((error) => {
        if (error) reject(error);
        else if (typeof port !== "number") reject(new Error("ephemeral port allocation failed"));
        else resolve(port);
      });
    });
  });
}

async function waitForReady(child, readyFile, baseUrl) {
  const deadline = Date.now() + 15000;
  while (Date.now() < deadline) {
    if (child.exitCode !== null) {
      let detail = "";
      try {
        detail = `: ${await readFile(readyFile, "utf8")}`;
      } catch {
        // The process may exit before it can write the bounded readiness file.
      }
      throw new Error(`local Platform server exited (${child.exitCode})${detail}`);
    }
    try {
      const ready = JSON.parse(await readFile(readyFile, "utf8"));
      if (ready.status !== "ready") throw new Error(`local Platform server was not ready: ${ready.status}`);
      const response = await fetch(`${baseUrl}/v1/spaces`);
      if (response.ok) return ready;
    } catch {
      // The readiness file and Uvicorn socket become available separately.
    }
    await new Promise((resolve) => setTimeout(resolve, 75));
  }
  throw new Error("local Platform server readiness timed out");
}

test("Console client reads the real local Catalog through an independent HTTP process", async () => {
  const before = await readFile(catalog);
  const tempRoot = await mkdtemp(path.join(os.tmpdir(), "knowledge-platform-console-http-"));
  const readyFile = path.join(tempRoot, "ready.json");
  const uploadSource = path.join(tempRoot, "console-upload.md");
  const uploadBytes = Buffer.from("Console local upload smoke\n", "utf8");
  await writeFile(uploadSource, uploadBytes);
  const port = await freePort();
  const baseUrl = `http://127.0.0.1:${port}`;
  const serverStderr = [];
  const child = spawn(
    python,
    [
      path.join(repoRoot, "backend/scripts/phase8_local_platform_process_server.py"),
      "--catalog", catalog,
      "--wiki-root", wikiRoot,
      "--temp-dir", path.join(tempRoot, "server-data"),
      "--port", String(port),
      "--ready-file", readyFile,
      "--semantic-markdown-db", path.join(tempRoot, "semantic-markdown.sqlite3"),
      "--upload-binding", `binding_console_smoke=${uploadSource}`,
    ],
    {
      cwd: repoRoot,
      env: { PATH: process.env.PATH, PYTHONPATH: path.join(repoRoot, "backend") },
      stdio: ["ignore", "ignore", "pipe"],
    },
  );
  child.stderr.on("data", (chunk) => {
    if (serverStderr.join("").length < 12000) serverStderr.push(chunk.toString());
  });
  try {
    const ready = await waitForReady(child, readyFile, baseUrl);
    assert.equal(ready.status, "ready");
    const client = createPlatformClient({ baseUrl });
    const spaces = await client.listSpaces();
    const datasets = await client.listDatasets({ spaceId: "space_kb_default" });
    const assets = await client.listAssets({ spaceId: "space_kb_default" });
    const semanticBefore = await client.listSemanticAssets({ spaceId: "space_kb_default" });
    let connectors;
    try {
      connectors = await client.listConnectors({ spaceId: "space_kb_default" });
    } catch (error) {
      throw new Error(`${error instanceof Error ? error.message : "Connector request failed"}; server_stderr=${serverStderr.join("")}`);
    }
    assert.equal(spaces.status, "ok");
    assert.equal(datasets.status, "ok");
    assert.equal(assets.status, "ok");
    assert.equal(semanticBefore.status, "ok");
    assert.deepEqual(semanticBefore.data.assets, []);
    assert.equal(connectors.status, "ok");
    assert.ok(connectors.data.connectors.some((item) => item.connector_key === "web_capture"));
    assert.equal(JSON.stringify(connectors).includes("credential_ref"), false);
    assert.equal(JSON.stringify(connectors).includes("config_json"), false);
    const connector = connectors.data.connectors.find((item) => item.connector_key === "web_capture");
    const authorizations = await client.listConnectorAuthorizations({ spaceId: "space_kb_default" });
    assert.equal(authorizations.status, "ok");
    assert.equal(JSON.stringify(authorizations).includes("credential_ref"), false);
    assert.equal(JSON.stringify(authorizations).includes("token_credential_ref"), false);
    const authorization = await client.authorizeConnector(connector.id, {
      space_id: "space_kb_default",
      mode: "user_reauthorize",
      idempotency_key: "console-connector-auth-smoke-1",
    });
    assert.equal(authorization.status, "ok");
    assert.equal(authorization.data.authorization.status, "awaiting_host_authorization");
    assert.equal(JSON.stringify(authorization).includes("access_token"), false);
    assert.equal(JSON.stringify(authorization).includes("external_url"), false);
    assert.equal(JSON.stringify(authorization).includes("/Users/"), false);
    const sourceItems = await client.listSourceItems({ spaceId: "space_kb_default", connectorId: connector.id });
    assert.equal(sourceItems.status, "ok");
    assert.ok(sourceItems.data.source_items.length > 0);
    assert.equal(JSON.stringify(sourceItems).includes("source_url"), false);
    assert.equal(JSON.stringify(sourceItems).includes("path_json"), false);
    assert.equal(JSON.stringify(sourceItems).includes("metadata_json"), false);
    const uploaded = await client.uploadAsset({
      asset_id: "asset_console_upload_smoke",
      space_id: "space_kb_default",
      title: "Console upload smoke",
      filename: "console-upload.md",
      mime_type: "text/markdown",
      binding_id: "binding_console_smoke",
      content_digest: digest(uploadBytes),
      idempotency_key: "console-upload-smoke-1",
    });
    assert.equal(uploaded.status, "ok");
    assert.equal(uploaded.data.upload.status, "staged");
    assert.equal(JSON.stringify(uploaded).includes(uploadSource), false);
    assert.equal(JSON.stringify(uploaded).includes("/Users/"), false);
    const semanticId = "dimension:console_smoke";
    const prepared = await client.prepareSemanticAsset({
      id: semanticId,
      space_id: "space_kb_default",
      type: "dimension",
      name: "Console smoke dimension",
      description: "A bounded local Console smoke definition",
      aliases: ["smoke dimension"],
      tags: ["local"],
      frontmatter: { source: "console-smoke" },
      body: "# Console smoke dimension\n",
    });
    assert.equal(prepared.status, "ok");
    assert.equal(prepared.data.asset.status, "waiting_for_confirmation");
    const semanticPending = await client.listSemanticAssets({ spaceId: "space_kb_default", status: "waiting_for_confirmation" });
    assert.ok(semanticPending.data.assets.some((item) => item.id === semanticId));
    const decided = await client.decideSemanticAsset(semanticId, {
      space_id: "space_kb_default",
      decision: "confirm",
      expected_status: "waiting_for_confirmation",
    });
    assert.equal(decided.status, "ok");
    assert.equal(decided.data.asset.status, "active");
    assert.equal(JSON.stringify(decided).includes("/Users/"), false);
    assert.ok(spaces.data.spaces.some((space) => space.id === "space_kb_default"));
    assert.ok(datasets.data.collections.some((collection) => collection.id === "dataset_kb_default"));
    assert.ok(assets.data.assets.some((asset) => asset.kind === "wiki_page"));
    const wikiAsset = assets.data.assets.find((asset) => asset.kind === "wiki_page");
    const assetRead = await client.readAsset(wikiAsset.id, {
      resource_uri: wikiAsset.source_uri,
      start: 0,
      end: 128,
    });
    assert.equal(assetRead.status, "ok");
    assert.equal(assetRead.data.start, 0);
    assert.ok(assetRead.data.end <= 128);
    assert.match(assetRead.data.content_digest, /^sha256:[0-9a-f]{64}$/);
    const derivatives = await client.listAssetDerivatives(wikiAsset.id);
    assert.equal(derivatives.status, "ok");
    assert.ok(derivatives.data.derivatives.some((item) => item.kind === "normalized_markdown"));
    const derivative = await client.readAssetDerivative(wikiAsset.id, "normalized_markdown", { start: 0, end: 128 });
    assert.equal(derivative.status, "ok");
    assert.match(derivative.data.resource_uri, /\/derivatives\/normalized_markdown$/);
    assert.ok(derivative.data.end <= 128);
    assert.equal(JSON.stringify(derivative).includes("/Users/"), false);

    const result = await client.wikiQuery({
      query: "Google Agent Skills",
      space_id: "space_kb_default",
      collection_id: "dataset_kb_default",
      limit: 2,
    });
    assert.equal(result.status, "ok");
    parseQueryResult(result);
    assert.ok(Array.isArray(result.evidence));
    const generic = await client.query({
      query: "Google Agent Skills",
      space_id: "space_kb_default",
      collection_id: "dataset_kb_default",
      capability_hint: "wiki_query",
      limit: 1,
    });
    assert.equal(generic.status, "ok");
    const mcpResources = await client.listMcpResources();
    assert.equal(mcpResources.resources.some((item) => item.uri.endsWith("/manifest")), true);
    assert.equal(mcpResources.resourceTemplates.length, 4);
    assert.equal(JSON.stringify(mcpResources).includes("/Users/"), false);
    const manifest = await client.readMcpResource("knowledge://spaces/space_kb_default/manifest");
    assert.equal(manifest.contents[0].uri, "knowledge://spaces/space_kb_default/manifest");
    assert.equal(manifest.contents[0].mimeType, "application/json");
    assert.equal(JSON.stringify(manifest).includes("/Users/"), false);
    const queryResult = await client.getQueryResult("query_result_qr_b91e28f0749140759ab99b2e");
    assert.equal(queryResult.status, "ok");
    assert.equal(queryResult.data.query_result.id, "query_result_qr_b91e28f0749140759ab99b2e");
    assert.match(queryResult.data.query_result.artifact_uri, /^knowledge:\/\//);
    assert.equal(JSON.stringify(queryResult).includes("/Users/"), false);
    const job = await client.getJob("processing_job_181e41da56c349a48b27845b");
    assert.equal(job.status, "ok");
    assert.equal(job.data.job.id, "processing_job_181e41da56c349a48b27845b");
    assert.equal(typeof job.data.job.progress, "number");
    assert.equal(JSON.stringify(job).includes("/Users/"), false);
    for (const evidence of result.evidence) {
      assert.match(evidence.resource_uri, /^knowledge:\/\//);
      assert.equal(Object.values(evidence).some((value) => typeof value === "string" && value.includes("/Users/")), false);
    }
  } finally {
    if (child.exitCode === null) {
      child.kill("SIGTERM");
      await new Promise((resolve) => child.once("exit", resolve));
    }
    const after = await readFile(catalog);
    assert.equal(digest(after), digest(before));
    await rm(tempRoot, { recursive: true, force: true });
  }
});
