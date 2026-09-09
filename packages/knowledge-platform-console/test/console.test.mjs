import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, readFile, readdir, rm, symlink, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { build } from "../scripts/build.mjs";

test("Console build is standalone, content-addressed, and non-activatable", async () => {
  const outputDir = await mkdtemp(path.join(os.tmpdir(), "knowledge-platform-console-"));
  try {
    await writeFile(path.join(outputDir, "stale-secret.txt"), "password=should-not-survive\n");
    const manifest = await build({ outputDir });
    assert.equal(manifest.status, "built_not_deployed");
    assert.equal(manifest.activation_allowed, false);
    for (const file of ["index.html", "app.mjs", "local-boundary.mjs", "display-boundary.mjs", "contracts.mjs", "manifest.json"]) {
      const content = await readFile(path.join(outputDir, file), "utf8");
      assert.ok(content.length > 0, file);
      assert.equal(content.includes("PuddingClaw"), false, file);
    }
    assert.deepEqual((await readdir(outputDir)).sort(), [
      "app.mjs",
      "contracts.mjs",
      "display-boundary.mjs",
      "index.html",
      "local-boundary.mjs",
      "manifest.json",
    ]);
    assert.match(manifest.files["contracts.mjs"], /^sha256:[0-9a-f]{64}$/);
    assert.deepEqual(
      manifest.surfaces.map((surface) => surface.id),
      ["knowledge", "analytics", "oauth", "imports", "sources", "schema", "results", "notifications"],
    );
    assert.ok(manifest.surfaces.every((surface) => surface.activation_allowed === false));
  } finally {
    await rm(outputDir, { recursive: true, force: true });
  }
});

test("Console surface manifest stays versioned and contract-backed", async () => {
  const outputDir = await mkdtemp(path.join(os.tmpdir(), "knowledge-platform-console-surface-"));
  try {
    const manifest = await build({ outputDir });
    assert.equal(manifest.format, "agent-knowledge-platform-console-dist/v1");
    assert.equal(new Set(manifest.surfaces.map((surface) => surface.id)).size, manifest.surfaces.length);
    for (const surface of manifest.surfaces) {
      assert.match(surface.id, /^[a-z][a-z-]+$/);
      assert.match(surface.contract, /^[a-z][a-z0-9_]+$/);
      assert.equal(surface.activation_allowed, false);
    }
    const persisted = JSON.parse(await readFile(path.join(outputDir, "manifest.json"), "utf8"));
    assert.deepEqual(persisted.surfaces, manifest.surfaces);
  } finally {
    await rm(outputDir, { recursive: true, force: true });
  }
});

test("Console build rejects an output path with a symlinked parent", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "knowledge-platform-console-parent-"));
  const target = await mkdtemp(path.join(os.tmpdir(), "knowledge-platform-console-target-"));
  try {
    const linkedParent = path.join(root, "linked-parent");
    await symlink(target, linkedParent, "dir");
    await assert.rejects(
      () => build({ outputDir: path.join(linkedParent, "dist") }),
      /symlink/,
    );
    assert.deepEqual(await readdir(target), []);
  } finally {
    await rm(root, { recursive: true, force: true });
    await rm(target, { recursive: true, force: true });
  }
});

test("Console shell renders text through DOM APIs instead of HTML injection", async () => {
  const source = await readFile(new URL("../src/app.mjs", import.meta.url), "utf8");
  assert.match(source, /textContent/);
  assert.doesNotMatch(source, /innerHTML/);
  assert.match(source, /collections\.data\?\.collections/);
  assert.match(source, /api\.listCollections/);
  assert.match(await readFile(new URL("../src/index.html", import.meta.url), "utf8"), /Spaces \/ Collections \/ Assets/);
  assert.match(source, /api\.listAssets/);
  assert.match(source, /api\.readAsset/);
  assert.match(source, /observeCollectionFreshness/);
  assert.match(source, /getQueryResult/);
  assert.match(source, /renderQueryResult/);
  assert.match(source, /databaseNl2Sql/);
  assert.match(source, /listDatabaseSchema/);
  assert.match(source, /executeDatabasePlan/);
  assert.match(source, /renderDatabaseSchema/);
  assert.match(source, /renderDatabasePlan/);
  assert.match(source, /renderDatabaseResult/);
  assert.match(source, /listMcpResources/);
  assert.match(source, /readMcpResource/);
  assert.match(source, /renderMcpResources/);
  assert.match(source, /listSemanticAssets/);
  assert.match(source, /renderSemanticAssets/);
  assert.match(source, /listConnectors/);
  assert.match(source, /listSourceItems/);
  assert.match(source, /renderConnectors/);
  assert.match(source, /renderSourceItems/);
  assert.match(source, /listConnectorAuthorizations/);
  assert.match(source, /authorizeConnector/);
  assert.match(source, /listNotifications/);
  assert.match(source, /renderNotifications/);
  assert.match(source, /listAssetBindingReviews/);
  assert.match(source, /renderAssetBindingReviews/);
  assert.match(source, /uploadAsset/);
  assert.match(source, /importPackage/);
  assert.match(source, /rebuildIndex/);
  assert.match(await readFile(new URL("../../knowledge-platform-console-contracts/src/index.mjs", import.meta.url), "utf8"), /query:.*\/v1\/query/);
  assert.doesNotMatch(source, /innerHTML/);
});

test("Console local shadow endpoint rejects non-loopback and unsafe origins", async () => {
  const { assertLocalShadowBaseUrl, isLocalShadowBaseUrl, localShadowApiUrlFromPageUrl } = await import("../src/local-boundary.mjs");
  const html = await readFile(new URL("../src/index.html", import.meta.url), "utf8");
  assert.match(html, /id="base-url" value="http:\/\/127\.0\.0\.1:8889"/);
  assert.equal(isLocalShadowBaseUrl("http://127.0.0.1:8889"), true);
  assert.equal(isLocalShadowBaseUrl("http://[::1]:8889"), true);
  assert.equal(isLocalShadowBaseUrl("", { pageHostname: "localhost" }), true);
  assert.equal(
    localShadowApiUrlFromPageUrl("http://127.0.0.1:18090/?api=http://127.0.0.1:18889"),
    "http://127.0.0.1:18889",
  );
  assert.equal(
    localShadowApiUrlFromPageUrl("http://127.0.0.1:8090/", { fallback: "http://127.0.0.1:8889" }),
    "http://127.0.0.1:8889",
  );
  assert.throws(
    () => localShadowApiUrlFromPageUrl("http://127.0.0.1:8090/?api=http://127.0.0.1:8889&api=http://127.0.0.1:8890"),
    TypeError,
  );
  assert.throws(
    () => localShadowApiUrlFromPageUrl("http://127.0.0.1:8090/?api=https://production.example"),
    TypeError,
  );
  for (const value of [
    "https://127.0.0.1:8889",
    "http://production.example",
    "http://127.0.0.1:8889/api",
    "http://user:pass@127.0.0.1:8889",
    "http://127.0.0.1:8889?redirect=1",
    "http://localhost.evil:8889",
  ]) {
    assert.equal(isLocalShadowBaseUrl(value), false, value);
    assert.throws(() => assertLocalShadowBaseUrl(value), TypeError);
  }
});

test("Console display boundary redacts arbitrary error, path, secret, and SQL text", async () => {
  const { formatDisplayError, safeDisplayText, safeDisplayValue } = await import("../src/display-boundary.mjs");
  const text = safeDisplayText("password=topsecret /Users/pet/private.md SELECT * FROM users");
  assert.equal(text.includes("topsecret"), false);
  assert.equal(text.includes("/Users/pet"), false);
  assert.equal(text.includes("SELECT"), false);
  assert.equal(safeDisplayText("db_password").includes("db_password"), false);
  assert.equal(formatDisplayError({ error: { code: "internal_error", message: "password=topsecret" } }, "请求失败"), "internal_error: 请求失败");
  assert.deepEqual(safeDisplayValue({ password: "topsecret", sql: "SELECT 1", nested: { path: "/Users/pet/x" } }), {
    "[sensitive-field-0]": "[redacted]",
    "[sensitive-field-1]": "[redacted]",
    nested: { path: "[local-reference]" },
  });
  const jsonText = safeDisplayText('{"password":"TOPSECRET","connection_string":"postgres://u:p@127.0.0.1/db"}');
  assert.equal(jsonText.includes("TOPSECRET"), false);
  assert.equal(jsonText.includes("postgres://"), false);
  assert.deepEqual(safeDisplayValue({ private_key: "TOPSECRET", credential_ref: "vault-secret-ref", auth_header: "Bearer TOPSECRET" }), {
    "[sensitive-field-0]": "[redacted]",
    "[sensitive-field-1]": "[redacted]",
    "[sensitive-field-2]": "[redacted]",
  });
});

test("Console Schema UI is read-only and does not expose connection facts", async () => {
  const source = await readFile(new URL("../src/app.mjs", import.meta.url), "utf8");
  const html = await readFile(new URL("../src/index.html", import.meta.url), "utf8");
  assert.match(html, /读取绑定 Schema/);
  assert.match(html, /不显示连接信息、文件路径、凭据或 SQL/);
  assert.match(source, /table\.columns/);
  assert.doesNotMatch(source, /connection_string/);
  assert.doesNotMatch(source, /database_url/);
});

test("Console semantic asset renderer is read-only and bounded", async () => {
  const source = await readFile(new URL("../src/app.mjs", import.meta.url), "utf8");
  assert.match(source, /Semantic Asset registry 未挂载/);
  assert.match(source, /item\.status/);
  assert.doesNotMatch(source, /prepareSemanticAsset\(/);
  assert.doesNotMatch(source, /decideSemanticAsset\(/);
});

test("Console source renderer excludes connector secret and path fields", async () => {
  const source = await readFile(new URL("../src/app.mjs", import.meta.url), "utf8");
  const html = await readFile(new URL("../src/index.html", import.meta.url), "utf8");
  assert.match(html, /Source Item 不展示外部 token/);
  assert.doesNotMatch(source, /credential_ref/);
  assert.doesNotMatch(source, /source_url/);
  assert.doesNotMatch(source, /path_json/);
  assert.doesNotMatch(source, /metadata_json/);
});

test("Console authorization UI stays host-managed and secret-free", async () => {
  const source = await readFile(new URL("../src/app.mjs", import.meta.url), "utf8");
  const html = await readFile(new URL("../src/index.html", import.meta.url), "utf8");
  assert.match(html, /宿主\/Vault 执行真正授权/);
  assert.match(html, /不接收 token、授权 URL 或凭据引用/);
  assert.match(source, /item\.status/);
  assert.doesNotMatch(source, /credential_ref/);
  assert.doesNotMatch(source, /token_credential_ref/);
  assert.doesNotMatch(source, /external_url/);
});

test("Console notification UI stays Space-scoped and does not expose inbox state", async () => {
  const source = await readFile(new URL("../src/app.mjs", import.meta.url), "utf8");
  const html = await readFile(new URL("../src/index.html", import.meta.url), "utf8");
  assert.match(html, /明确绑定到当前 Space/);
  assert.match(source, /已读状态由消费方维护/);
  assert.match(source, /item\.resource_uri/);
  assert.doesNotMatch(source, /markNotificationRead/);
  assert.doesNotMatch(source, /read_at/);
});

test("Console upload/import UI stays in the host-binding staging boundary", async () => {
  const source = await readFile(new URL("../src/app.mjs", import.meta.url), "utf8");
  const html = await readFile(new URL("../src/index.html", import.meta.url), "utf8");
  assert.match(html, /宿主预先登记的 binding ID/);
  assert.doesNotMatch(source, /source_path/);
  assert.doesNotMatch(source, /physical_path/);
  assert.doesNotMatch(source, /raw_markdown/);
  assert.match(html, /成功后仍需后续 Processing\/Index 流程才能激活/);
  assert.match(html, /候选完成后不会自动激活/);
});

test("Console Asset binding review UI is read-only and path-free", async () => {
  const source = await readFile(new URL("../src/app.mjs", import.meta.url), "utf8");
  const html = await readFile(new URL("../src/index.html", import.meta.url), "utf8");
  assert.match(html, /人工批准仍须在宿主命令行显式执行/);
  assert.match(source, /复制 review ID/);
  assert.doesNotMatch(source, /approveReview/);
  assert.doesNotMatch(source, /prepareBinding/);
  assert.doesNotMatch(source, /source_path/);
  assert.doesNotMatch(source, /physical_path/);
});
