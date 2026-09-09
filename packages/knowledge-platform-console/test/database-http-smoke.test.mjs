import test from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { spawn } from "node:child_process";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createPlatformClient } from "../../knowledge-platform-console-contracts/src/index.mjs";

const packageDir = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(packageDir, "../../..");
const catalog = path.join(repoRoot, "artifacts/phase0b-local-catalog/knowledge-platform.sqlite3");
const python = path.join(repoRoot, "backend/.venv/bin/python");
const spaceId = "space_kb_default";
const datasetId = "database_insight_data_vehicle_model_base";

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
    if (child.exitCode !== null) throw new Error(`local database Platform server exited (${child.exitCode})`);
    try {
      const ready = JSON.parse(await readFile(readyFile, "utf8"));
      if (ready.status !== "ready") throw new Error(`local database Platform server was not ready: ${ready.status}`);
      const response = await fetch(`${baseUrl}/v1/spaces`);
      if (response.ok) return ready;
    } catch {
      // The readiness file and Uvicorn socket become available separately.
    }
    await new Promise((resolve) => setTimeout(resolve, 75));
  }
  throw new Error("local database Platform server readiness timed out");
}

function digest(bytes) {
  return `sha256:${createHash("sha256").update(bytes).digest("hex")}`;
}

test("Console client exercises local Database Schema and two-phase query through an independent process", async () => {
  const before = digest(await readFile(catalog));
  const tempRoot = await mkdtemp(path.join(os.tmpdir(), "knowledge-platform-console-database-http-"));
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
    assert.equal(ready.status, "ready");
    const client = createPlatformClient({ baseUrl });
    const schema = await client.listDatabaseSchema({ spaceId, datasetId });
    assert.equal(schema.status, "ok");
    assert.ok(schema.data.tables.length >= 1);
    assert.equal(JSON.stringify(schema).includes("/Users/"), false);
    assert.equal(JSON.stringify(schema).includes("connection"), false);
    assert.equal(JSON.stringify(schema).includes("password"), false);

    const plan = await client.databaseNl2Sql({
      space_id: spaceId,
      dataset_id: datasetId,
      question: "统计本地车型能源类型数量",
    });
    assert.equal(plan.status, "ok");
    const queryPlan = plan.data.query_plan;
    assert.equal(typeof queryPlan.query_plan_id, "string");
    const result = await client.executeDatabasePlan(queryPlan.query_plan_id, {
      space_id: spaceId,
      page_size: 20,
      expected_sql_hash: plan.data.sql_hash,
    });
    assert.equal(result.status, "ok");
    assert.ok(result.data.row_count >= 1);
    assert.equal(JSON.stringify(result).includes("SELECT"), false);
    assert.equal(JSON.stringify(result).includes("/Users/"), false);
    assert.equal(digest(await readFile(catalog)), before);
  } catch (error) {
    throw new Error(`${error instanceof Error ? error.message : "database smoke failed"}; server_stderr=${stderr.join("")}`);
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
