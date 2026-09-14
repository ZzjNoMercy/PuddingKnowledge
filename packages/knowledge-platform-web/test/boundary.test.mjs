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
  assert.match(proxy, /DEFAULT_REQUEST_BODY_LIMIT = 1024 \* 1024/);
  assert.match(proxy, /AUTHORING_REQUEST_BODY_LIMIT = 32 \* 1024 \* 1024/);
  assert.match(proxy, /AUTHORING_ACTION_PATHS/);
  assert.match(proxy, /readRequestBodyWithinLimit/);
  assert.match(proxy, /chunk\.byteLength/);
  assert.match(proxy, /RequestBodyTooLargeError/);
  assert.doesNotMatch(proxy, /authorization|cookie/i);
});

test("request body helper enforces actual stream bytes without Content-Length", async () => {
  const proxy = await import(path.join(root, "src/lib/platform-proxy.ts"));
  const withinLimit = await proxy.readRequestBodyWithinLimit(
    new Request("http://localhost", { body: "你好", method: "POST" }),
    new TextEncoder().encode("你好").byteLength,
  );
  assert.equal(withinLimit, "你好");

  await assert.rejects(
    proxy.readRequestBodyWithinLimit(
      new Request("http://localhost", { body: "你好", method: "POST" }),
      "你".length,
    ),
    proxy.RequestBodyTooLargeError,
  );

  const stream = new ReadableStream({
    start(controller) {
      controller.enqueue(new TextEncoder().encode("a".repeat(1024 * 1024)));
      controller.enqueue(new TextEncoder().encode("b"));
      controller.close();
    },
  });
  await assert.rejects(
    proxy.readRequestBodyWithinLimit({ body: stream }, 1024 * 1024),
    proxy.RequestBodyTooLargeError,
  );
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

test("asset resource parsing derives only exact same-origin 64-hex URLs", async () => {
  const platform = await import(path.join(root, "src/lib/asset-resources.ts"));
  const assetId = "document-42";
  const digest = "a".repeat(64);
  const resources = platform.parseAssetResources(assetId, {
    resources: [
      { id: digest, name: "photo.png", mime_type: "image/png", size_bytes: 12, url: `/v1/assets/${assetId}/resources/${digest}` },
      { id: "b".repeat(63), name: "bad.bin", mime_type: "application/octet-stream", size_bytes: 2, url: `/v1/assets/${assetId}/resources/${"b".repeat(63)}` },
      { id: "c".repeat(64), name: "external.bin", mime_type: "application/octet-stream", size_bytes: 2, url: "https://evil.example/resource" },
    ]
  }, "http://localhost");
  assert.deepEqual(resources, [{ id: digest, name: "photo.png", mime_type: "image/png", size_bytes: 12, url: `http://localhost/v1/assets/${assetId}/resources/${digest}` }]);
  assert.equal(platform.safeAssetResourceUrl(assetId, digest, `http://localhost/v1/assets/${assetId}/resources/${digest}?redirect=https://evil.example`, "http://localhost"), null);
  assert.equal(platform.isInlineImageMimeType("image/webp"), true);
  assert.equal(platform.isInlineImageMimeType("image/svg+xml"), false);
});

test("resource proxy preserves only binary-safe response headers", async () => {
  const proxy = await readFile(path.join(root, "src/lib/platform-proxy.ts"), "utf8");
  for (const header of ["cache-control", "content-disposition", "content-security-policy", "referrer-policy", "x-content-type-options"]) {
    assert.match(proxy, new RegExp(`\\"${header}\\"`));
  }
  assert.doesNotMatch(proxy, /headers:\s*response\.headers/);
  assert.doesNotMatch(proxy, /authorization|cookie/i);
});
