import assert from "node:assert/strict";
import { readFile, readdir } from "node:fs/promises";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

async function sourceFiles(directory) {
  const entries = await readdir(directory, { withFileTypes: true });
  const files = [];
  for (const entry of entries) {
    const target = path.join(directory, entry.name);
    if (entry.isDirectory()) files.push(...await sourceFiles(target));
    else if (/\.(?:ts|tsx|mjs)$/.test(entry.name)) files.push(target);
  }
  return files;
}

test("product UI has no legacy Claw frontend or API dependency", async () => {
  const files = await sourceFiles(path.join(root, "src"));
  const source = (await Promise.all(files.map((file) => readFile(file, "utf8")))).join("\n");
  assert.doesNotMatch(source, /from\s+["'][^"']*(?:frontend\/|lib\/store|components\/layout\/(?:Navbar|Sidebar))/);
  assert.doesNotMatch(source, /fetch\s*\(\s*["'`]\/api(?:\/|["'`])/);
  assert.doesNotMatch(source, /http:\/\/(?:127\.0\.0\.1|localhost):8888/);
  assert.match(source, /createPlatformClient\(\{ baseUrl: "" \}\)/);
});

test("same-origin BFF only accepts an explicit loopback Platform origin", async () => {
  const proxy = await readFile(path.join(root, "src/lib/platform-proxy.ts"), "utf8");
  assert.match(proxy, /LOOPBACK_HOSTS/);
  assert.match(proxy, /parsed\.protocol !== "http:"/);
  assert.match(proxy, /PLATFORM_PATH\.test\(pathname\)/);
  assert.match(proxy, /target\.pathname !== pathname/);
  assert.doesNotMatch(proxy.match(/const PLATFORM_PATH = ([^;]+)/)?.[1] || "", /%/);
  assert.match(proxy, /contentLength > 1024 \* 1024/);
  assert.doesNotMatch(proxy, /authorization|cookie/i);
});

test("launcher starts the product Web package instead of the diagnostics Console", async () => {
  const launcher = await readFile(path.resolve(root, "../../scripts/start-knowledge-local.sh"), "utf8");
  assert.match(launcher, /packages\/knowledge-platform-web/);
  assert.match(launcher, /PLATFORM_API_URL="\$API_URL"/);
  assert.match(launcher, /\/knowledge/);
  assert.match(launcher, /\$CONSOLE_ORIGIN\/knowledge/);
  assert.match(launcher, /\$CONSOLE_ORIGIN\/v1\/spaces/);
  assert.doesNotMatch(launcher, /python[^\n]*-m http\.server/);
});

test("product navigation exposes the migrated Knowledge surfaces", async () => {
  const workspace = await readFile(path.join(root, "src/components/KnowledgeWorkspace.tsx"), "utf8");
  for (const label of ["概览", "资料库", "搜索", "知识来源", "LLM Wiki", "任务中心", "智能问数"]) {
    assert.match(workspace, new RegExp(label));
  }
  assert.match(workspace, /platformClient\.readAsset/);
  assert.match(workspace, /platformClient\.search/);
});
