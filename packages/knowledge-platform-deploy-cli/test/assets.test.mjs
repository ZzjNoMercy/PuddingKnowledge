import test from "node:test";
import assert from "node:assert/strict";
import { chmod, mkdtemp, mkdir, readFile, rm, symlink, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import os from "node:os";
import path from "node:path";
import { spawn } from "node:child_process";

const { files } = createRequire(import.meta.url)("../package.json");
const supervisorPath = path.resolve(import.meta.dirname, "../assets/platform-infra.sh");

async function runSupervisor(home, bin, command = "plan", argsFile = null) {
  const child = spawn(supervisorPath, [command], {
    env: {
      ...process.env,
      PATH: `${bin}:${process.env.PATH}`,
      PUDDINGKNOWLEDGE_HOME: home,
      PUDDINGKNOWLEDGE_POSTGRES_USER: "test-user",
      PUDDINGKNOWLEDGE_POSTGRES_PASSWORD: "test-password",
      PUDDINGKNOWLEDGE_MINIO_ROOT_USER: "test-minio",
      PUDDINGKNOWLEDGE_MINIO_ROOT_PASSWORD: "test-minio-password",
      PUDDINGKNOWLEDGE_API_IMAGE: "test/api:local",
      PUDDINGKNOWLEDGE_WORKER_IMAGE: "test/worker:local",
      PUDDINGKNOWLEDGE_CONSOLE_IMAGE: "test/console:local",
      ...(argsFile ? { FAKE_DOCKER_ARGS: argsFile } : {}),
    },
    stdio: ["ignore", "pipe", "pipe"],
  });
  let stderr = "";
  child.stderr.on("data", (chunk) => { stderr += chunk; });
  const code = await new Promise((resolve) => child.once("close", resolve));
  return { code, stderr };
}

test("Platform Compose asset is independent, secret-free, and explicit about unreleased images", async () => {
  const compose = await readFile(new URL("../assets/compose.platform.yml", import.meta.url), "utf8");
  assert.equal(compose.includes("PuddingClaw"), false);
  assert.equal(compose.includes("puddingclaw"), false);
  assert.match(compose, /POSTGRES_PASSWORD:\s*\$\{PUDDINGKNOWLEDGE_POSTGRES_PASSWORD:\?set/);
  assert.match(compose, /MINIO_ROOT_PASSWORD:\s*\$\{PUDDINGKNOWLEDGE_MINIO_ROOT_PASSWORD:\?set/);
  assert.match(compose, /PUDDINGKNOWLEDGE_API_IMAGE:\?set/);
  assert.match(compose, /PUDDINGKNOWLEDGE_WORKER_IMAGE:\?set/);
  assert.match(compose, /PUDDINGKNOWLEDGE_CONSOLE_IMAGE:\?set/);
  assert.equal(/^\s+[A-Z_]*PASSWORD:(?!\s*\$\{)[^\n]*/im.test(compose), false);
  assert.ok(files.includes("assets"));
});

test("Platform infrastructure supervisor is an independent explicit Compose boundary", async () => {
  const supervisor = await readFile(new URL("../assets/platform-infra.sh", import.meta.url), "utf8");
  assert.match(supervisor, /docker compose --project-name/);
  assert.match(supervisor, /PUDDINGKNOWLEDGE_HOME/);
  assert.match(supervisor, /platform-infra\.sh (plan|up|down|status)/);
  assert.doesNotMatch(supervisor, /PuddingClaw|puddingclaw|start-local-infra\.sh/);
});

test("Platform infrastructure supervisor rejects symlinked persistent directories", async () => {
  const home = await mkdtemp(path.join(os.tmpdir(), "knowledge-platform-supervisor-"));
  const bin = await mkdtemp(path.join(os.tmpdir(), "knowledge-platform-supervisor-bin-"));
  const target = await mkdtemp(path.join(os.tmpdir(), "knowledge-platform-supervisor-target-"));
  try {
    await mkdir(path.join(home, "infrastructure", "milvus"), { recursive: true });
    await mkdir(path.join(home, "infrastructure", "postgres"));
    await mkdir(path.join(home, "infrastructure", "milvus", "etcd"));
    await mkdir(path.join(home, "infrastructure", "milvus", "minio"));
    await symlink(target, path.join(home, "infrastructure", "milvus", "data"));
    await writeFile(path.join(bin, "docker"), "#!/bin/sh\nexit 0\n", { mode: 0o700 });
    await chmod(path.join(bin, "docker"), 0o700);
    const result = await runSupervisor(home, bin);
    assert.equal(result.code, 2);
    assert.match(result.stderr, /must be a real directory/);
  } finally {
    await rm(home, { recursive: true, force: true });
    await rm(bin, { recursive: true, force: true });
    await rm(target, { recursive: true, force: true });
  }
});

test("Platform infrastructure supervisor rejects root and trailing-slash Home paths", async () => {
  const target = await mkdtemp(path.join(os.tmpdir(), "knowledge-platform-supervisor-home-target-"));
  const parent = await mkdtemp(path.join(os.tmpdir(), "knowledge-platform-supervisor-home-parent-"));
  const link = path.join(parent, "platform-home");
  try {
    await symlink(target, link);
    const rootResult = await runSupervisor("/", parent);
    assert.equal(rootResult.code, 2);
    assert.match(rootResult.stderr, /dedicated directory/);
    const trailingResult = await runSupervisor(`${link}/`, parent);
    assert.equal(trailingResult.code, 2);
    assert.match(trailingResult.stderr, /dedicated directory/);
  } finally {
    await rm(parent, { recursive: true, force: true });
    await rm(target, { recursive: true, force: true });
  }
});

test("Platform infrastructure supervisor maps explicit lifecycle commands to Platform Compose only", async () => {
  const home = await mkdtemp(path.join(os.tmpdir(), "knowledge-platform-supervisor-lifecycle-"));
  const bin = await mkdtemp(path.join(os.tmpdir(), "knowledge-platform-supervisor-lifecycle-bin-"));
  const argsFile = path.join(bin, "docker-args.txt");
  try {
    await mkdir(path.join(home, "infrastructure", "milvus", "etcd"), { recursive: true });
    await mkdir(path.join(home, "infrastructure", "postgres"));
    await mkdir(path.join(home, "infrastructure", "milvus", "minio"));
    await mkdir(path.join(home, "infrastructure", "milvus", "data"));
    await writeFile(
      path.join(bin, "docker"),
      "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"${FAKE_DOCKER_ARGS}\"\n",
      { mode: 0o700 },
    );
    for (const [command, expected] of [["up", ["up", "-d"]], ["down", ["down"]], ["status", ["ps"]]]) {
      const result = await runSupervisor(home, bin, command, argsFile);
      assert.equal(result.code, 0, `${command}: ${result.stderr}`);
      const args = (await readFile(argsFile, "utf8")).trim().split(/\r?\n/);
      assert.deepEqual(args.slice(0, 5), ["compose", "--project-name", "puddingknowledge", "--file", path.join(path.dirname(supervisorPath), "compose.platform.yml")]);
      assert.deepEqual(args.slice(-expected.length), expected);
      assert.doesNotMatch(args.join(" ").toLowerCase(), /docker-compose\.infra|start-local-infra/);
    }
  } finally {
    await rm(home, { recursive: true, force: true });
    await rm(bin, { recursive: true, force: true });
  }
});
