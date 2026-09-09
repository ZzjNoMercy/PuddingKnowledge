import test from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdtemp, readFile, readdir, rm, stat } from "node:fs/promises";
import { spawn } from "node:child_process";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createPlatformClient } from "../../knowledge-platform-console-contracts/src/index.mjs";

const packageDir = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(packageDir, "../../..");
const catalog = path.join(repoRoot, "artifacts/phase0b-local-catalog/knowledge-platform.sqlite3");
const collectionRoot = path.join(repoRoot, "artifacts/phase9-local-database-vanna-replay/vanna/collections");
const python = path.join(repoRoot, "backend/.venv/bin/python");
const spaceId = "space_kb_default";
const datasetId = "database_insight_data_vehicle_model_base";

function canonicalCollectionName(packageManifest) {
  const safeId = String(packageManifest.id || "package").replace(/[^A-Za-z0-9._-]+/g, "-").replace(/^-+|-+$/g, "") || "package";
  const revisionHash = createHash("sha256").update(String(packageManifest.package_revision || "")).digest("hex").slice(0, 16);
  return `puddingknowledge_vanna_${safeId}_${revisionHash}`;
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
  const deadline = Date.now() + 60000;
  while (Date.now() < deadline) {
    if (child.exitCode !== null) throw new Error(`local Vanna Platform server exited (${child.exitCode})`);
    try {
      const ready = JSON.parse(await readFile(readyFile, "utf8"));
      if (ready.status !== "ready") throw new Error(`local Vanna Platform server was not ready: ${ready.status}`);
      const response = await fetch(`${baseUrl}/v1/spaces`);
      if (response.ok) return ready;
    } catch {
      // Readiness file and Uvicorn socket become available separately.
    }
    await new Promise((resolve) => setTimeout(resolve, 75));
  }
  throw new Error("local Vanna Platform server readiness timed out");
}

function digest(bytes) {
  return `sha256:${createHash("sha256").update(bytes).digest("hex")}`;
}

async function mcpCall(baseUrl, id, name, argumentsValue) {
  const response = await fetch(`${baseUrl}/mcp`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      jsonrpc: "2.0",
      id,
      method: "tools/call",
      params: { name, arguments: argumentsValue },
    }),
  });
  return { status: response.status, payload: await response.json() };
}

test("Console and MCP consume the local Vanna Collection provider", async () => {
  const entries = await readdir(collectionRoot, { withFileTypes: true });
  const candidates = entries.filter((entry) => entry.isDirectory()).map((entry) => entry.name);
  assert.equal(candidates.length, 1);
  assert.equal(entries.some((entry) => entry.isSymbolicLink()), false);
  const packageManifest = JSON.parse(await readFile(path.join(repoRoot, "artifacts/phase9-local-database-vanna-replay/package/package-manifest.json"), "utf8"));
  const expectedCollectionName = canonicalCollectionName(packageManifest);
  assert.deepEqual(candidates, [expectedCollectionName]);
  const collection = path.join(collectionRoot, expectedCollectionName);
  const collectionManifest = JSON.parse(await readFile(path.join(collection, "collection-manifest.json"), "utf8"));
  assert.equal(collectionManifest.collection_name, expectedCollectionName);
  assert.equal(collectionManifest.package_revision, packageManifest.package_revision);
  assert.equal(collectionManifest.active, false);
  assert.equal(collectionManifest.activation_allowed, false);
  assert.equal(collectionManifest.provider_io_performed, false);
  assert.equal(collectionManifest.legacy_collection_read, false);
  assert.deepEqual(Object.keys(collectionManifest.file_digests).sort(), ["ddl.jsonl", "documentation.jsonl", "entities.jsonl", "sql_examples.jsonl"]);
  for (const file of ["collection-manifest.json", "ddl.jsonl", "documentation.jsonl", "entities.jsonl", "sql_examples.jsonl"]) {
    assert.equal((await stat(path.join(collection, file))).isFile(), true);
  }
  for (const file of Object.keys(collectionManifest.file_digests)) {
    assert.equal(collectionManifest.file_digests[file], digest(await readFile(path.join(collection, file))));
  }
  const collectionReport = JSON.parse(await readFile(path.join(collectionRoot, "..", "vanna-collection-shadow-report.json"), "utf8"));
  assert.equal(collectionManifest.input_digest, collectionReport.collection.input_digest);
  assert.equal(collectionManifest.package_revision, collectionReport.collection.package_revision);
  const before = digest(await readFile(catalog));
  const tempRoot = await mkdtemp(path.join(os.tmpdir(), "knowledge-platform-console-vanna-http-"));
  const readyFile = path.join(tempRoot, "ready.json");
  const port = await freePort();
  const baseUrl = `http://127.0.0.1:${port}`;
  const childEnv = { PATH: process.env.PATH, PYTHONPATH: path.join(repoRoot, "backend") };
  const passwordEnv = "PUDDINGCLAW_CANONICAL_DB_PASSWORD";
  if (process.env[passwordEnv] !== undefined) childEnv[passwordEnv] = process.env[passwordEnv];
  const child = spawn(
    python,
    [
      path.join(repoRoot, "backend/scripts/phase8_local_platform_process_server.py"),
      "--catalog", catalog,
      "--wiki-root", path.join(repoRoot, "docs"),
      "--temp-dir", path.join(tempRoot, "server-data"),
      "--port", String(port),
      "--ready-file", readyFile,
      "--database-mode",
      "--database-vanna-collection", collection,
      "--database-vanna-collection-name", expectedCollectionName,
      "--database-vanna-package-revision", packageManifest.package_revision,
      "--database-vanna-input-digest", collectionReport.collection.input_digest,
      "--db-host", "127.0.0.1",
      "--db-port", "5432",
      "--db-name", "insight_data",
      "--db-user", "pet",
      "--db-password-env", passwordEnv,
    ],
    { cwd: repoRoot, env: childEnv, stdio: ["ignore", "ignore", "pipe"] },
  );
  const stderr = [];
  child.stderr.on("data", (chunk) => {
    if (stderr.join("").length < 12000) stderr.push(chunk.toString());
  });
  try {
    const ready = await waitForReady(child, readyFile, baseUrl);
    assert.equal(ready.database_vanna_collection_bound, true);
    const client = createPlatformClient({ baseUrl });
    const plan = await client.databaseNl2Sql({
      space_id: spaceId,
      dataset_id: datasetId,
      question: "统计本地车型能源类型数量",
    });
    assert.equal(plan.status, "ok");
    assert.equal(plan.provenance?.provider_versions?.nl2sql, "local-file-backed-collection-shadow");
    assert.equal(plan.data.query_plan.validation.readonly, true);
    assert.equal(plan.data.query_plan.validation.allowed_tables, true);
    const result = await client.executeDatabasePlan(plan.data.query_plan.query_plan_id, {
      space_id: spaceId,
      page_size: 20,
      expected_sql_hash: plan.data.sql_hash,
    });
    assert.equal(result.status, "ok");
    assert.equal(result.data.row_count, 14);
    assert.equal(JSON.stringify(result).includes("SELECT"), false);
    assert.equal(JSON.stringify(result).includes("/Users/"), false);

    const mcpPlan = await mcpCall(baseUrl, "vanna-mcp-generate", "database_nl2sql", {
      space_id: spaceId,
      dataset_id: datasetId,
      question: "统计本地车型能源类型数量",
    });
    assert.equal(mcpPlan.status, 200);
    const mcpStructured = mcpPlan.payload.result?.structuredContent;
    assert.equal(mcpStructured?.status, "ok");
    assert.equal(mcpStructured?.provenance?.provider_versions?.nl2sql, "local-file-backed-collection-shadow");
    const mcpExecute = await mcpCall(baseUrl, "vanna-mcp-execute", "database_execute_readonly", {
      space_id: spaceId,
      query_plan_id: mcpStructured.data.query_plan.query_plan_id,
      expected_sql_hash: mcpStructured.data.sql_hash,
      page_size: 20,
    });
    assert.equal(mcpExecute.status, 200);
    assert.equal(mcpExecute.payload.result?.structuredContent?.status, "ok");
    assert.equal(mcpExecute.payload.result?.structuredContent?.data?.row_count, 14);
    assert.equal(JSON.stringify(mcpExecute.payload).includes("SELECT"), false);
    assert.equal(digest(await readFile(catalog)), before);
  } catch (error) {
    throw new Error(`${error instanceof Error ? error.message : "Vanna Console smoke failed"}; server_stderr=${stderr.join("")}`);
  } finally {
    if (child.exitCode === null) {
      await new Promise((resolve) => {
        child.once("exit", resolve);
        child.kill("SIGTERM");
      });
    }
    await rm(tempRoot, { recursive: true, force: true });
  }
});
