import { createHash } from "node:crypto";
import { lstat, mkdir, readFile, readdir, readlink, rm, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const packageRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const defaultOutput = path.join(packageRoot, "dist");

const SURFACES = Object.freeze([
  Object.freeze({ id: "knowledge", contract: "knowledge_query" }),
  Object.freeze({ id: "analytics", contract: "database_query_plan" }),
  Object.freeze({ id: "oauth", contract: "host_managed_authorization" }),
  Object.freeze({ id: "imports", contract: "asset_and_package_staging" }),
  Object.freeze({ id: "sources", contract: "connector_source_items" }),
  Object.freeze({ id: "schema", contract: "binding_scoped_database_schema" }),
  Object.freeze({ id: "results", contract: "query_result_metadata" }),
  Object.freeze({ id: "notifications", contract: "space_scoped_events" }),
]);

function digest(bytes) {
  return createHash("sha256").update(bytes).digest("hex");
}

async function assertNoSymlinkComponents(value) {
  const absolute = path.resolve(value);
  const { root } = path.parse(absolute);
  let current = root;
  for (const component of absolute.slice(root.length).split(path.sep).filter(Boolean)) {
    current = path.join(current, component);
    try {
      if ((await lstat(current)).isSymbolicLink()) {
        const target = path.resolve(path.dirname(current), await readlink(current));
        const systemAlias = (current === "/var" && target === "/private/var") || (current === "/tmp" && target === "/private/tmp");
        if (!systemAlias) throw new Error("Console build output path contains a symlink");
      }
    } catch (error) {
      if (error?.code === "ENOENT") break;
      throw error;
    }
  }
}

export async function build({ outputDir = defaultOutput } = {}) {
  await assertNoSymlinkComponents(outputDir);
  const contractPath = path.resolve(packageRoot, "..", "knowledge-platform-console-contracts", "src", "index.mjs");
  const assets = {
    "index.html": await readFile(path.join(packageRoot, "src", "index.html")),
    "app.mjs": await readFile(path.join(packageRoot, "src", "app.mjs")),
    "local-boundary.mjs": await readFile(path.join(packageRoot, "src", "local-boundary.mjs")),
    "display-boundary.mjs": await readFile(path.join(packageRoot, "src", "display-boundary.mjs")),
    "contracts.mjs": await readFile(contractPath),
  };
  try {
    const metadata = await lstat(outputDir);
    if (metadata.isSymbolicLink() || !metadata.isDirectory()) throw new Error("Console build output must be a real directory");
  } catch (error) {
    if (error?.code !== "ENOENT") throw error;
    await mkdir(outputDir, { recursive: true, mode: 0o755 });
  }
  for (const entry of await readdir(outputDir)) await rm(path.join(outputDir, entry), { recursive: true, force: true });
  for (const [name, bytes] of Object.entries(assets)) await writeFile(path.join(outputDir, name), bytes, { mode: 0o644 });
  const manifest = {
    format: "agent-knowledge-platform-console-dist/v1",
    status: "built_not_deployed",
    activation_allowed: false,
    surfaces: SURFACES.map((surface) => ({ ...surface, activation_allowed: false })),
    files: Object.fromEntries(Object.entries(assets).map(([name, bytes]) => [name, `sha256:${digest(bytes)}`])),
  };
  await writeFile(path.join(outputDir, "manifest.json"), `${JSON.stringify(manifest, null, 2)}\n`, { mode: 0o644 });
  return manifest;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const manifest = await build();
  process.stdout.write(`${JSON.stringify(manifest, null, 2)}\n`);
}
