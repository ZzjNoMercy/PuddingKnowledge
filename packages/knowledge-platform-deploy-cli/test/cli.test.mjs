import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, readFile, readdir, realpath, rm, mkdir, stat, symlink, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { validateComposeAsset, verifyRuntimeBundle } from "../src/state.mjs";

const root = path.resolve(import.meta.dirname, "..");
const cli = path.join(root, "src", "cli.mjs");
const TEMP_ROOT = await realpath(os.tmpdir());

async function tempHome() {
  return mkdtemp(path.join(TEMP_ROOT, "knowledge-platform-cli-"));
}

async function run(args) {
  const child = spawn(process.execPath, [cli, ...args], { stdio: ["ignore", "pipe", "pipe"] });
  let stdout = "";
  let stderr = "";
  child.stdout.on("data", (chunk) => { stdout += chunk; });
  child.stderr.on("data", (chunk) => { stderr += chunk; });
  const code = await new Promise((resolve) => child.once("close", resolve));
  return { code, stdout, stderr };
}

test("Platform CLI init/status stay independent and non-activatable", async () => {
  const home = await tempHome();
  try {
    const initialized = await run(["init", "--home", home, "--json"]);
    assert.equal(initialized.code, 0, initialized.stdout + initialized.stderr);
    const init = JSON.parse(initialized.stdout);
    assert.equal(init.status, "initialized");
    assert.equal(init.config.activation_allowed, false);
    assert.equal(init.config.infrastructure.owner, "puddingknowledge");
    assert.equal(init.config.infrastructure.process_started, false);
    assert.equal((await stat(path.join(home, "infrastructure/milvus/data"))).isDirectory(), true);
    const current = await run(["status", "--home", home, "--json"]);
    assert.equal(current.code, 0);
    assert.equal(JSON.parse(current.stdout).status, "initialized");
    assert.equal(JSON.parse(await readFile(path.join(home, "platform.json"), "utf8")).service, "puddingknowledge");
  } finally {
    await rm(home, { recursive: true, force: true });
  }
});

test("Platform init rejects a Home whose existing parent is a symlink", async () => {
  const rootHome = await tempHome();
  const actualParent = path.join(rootHome, "actual-parent");
  const aliasParent = path.join(rootHome, "alias-parent");
  const requestedHome = path.join(aliasParent, "home");
  try {
    await mkdir(actualParent);
    await symlink(actualParent, aliasParent);
    const result = await run(["init", "--home", requestedHome, "--json"]);
    assert.equal(result.code, 1);
    assert.equal(JSON.parse(result.stdout).error_code, "invalid_home");
    await assert.rejects(stat(requestedHome), { code: "ENOENT" });
  } finally {
    await rm(rootHome, { recursive: true, force: true });
  }
});

test("Platform operations reject configs that are not owned initialized Platform state", async () => {
  const home = await tempHome();
  const outputParent = await tempHome();
  try {
    await run(["init", "--home", home]);
    for (const config of [
      { schema_version: 1, service: "puddingclaw", initialized: true },
      { service: "puddingknowledge", initialized: true },
      { schema_version: 1, service: "puddingknowledge", initialized: false },
      { schema_version: 1, service: "puddingknowledge", initialized: true, activation_allowed: true, infrastructure: { owner: "puddingknowledge" } },
      { schema_version: 1, service: "puddingknowledge", initialized: true, activation_allowed: false, infrastructure: { owner: "puddingclaw" } },
    ]) {
      await writeFile(path.join(home, "platform.json"), JSON.stringify(config));
      const status = await run(["status", "--home", home, "--json"]);
      assert.equal(status.code, 1);
      assert.equal(JSON.parse(status.stdout).error_code, "invalid_state");
      const backup = await run(["backup", "--home", home, "--output", path.join(outputParent, "snapshot"), "--apply", "--json"]);
      assert.equal(backup.code, 1);
      assert.equal(JSON.parse(backup.stdout).error_code, "invalid_state");
    }
  } finally {
    await rm(home, { recursive: true, force: true });
    await rm(outputParent, { recursive: true, force: true });
  }
});

test("Platform deployment removes a temporary state file when its destination is a directory", async () => {
  const home = await tempHome();
  const bundle = await mkdtemp(path.join(TEMP_ROOT, "knowledge-platform-bundle-state-dir-"));
  try {
    await run(["init", "--home", home]);
    await mkdir(path.join(bundle, "bin"), { recursive: true });
    const runtime = "runtime\n";
    await writeFile(path.join(bundle, "bin", "platform"), runtime);
    await writeFile(path.join(bundle, "manifest.json"), JSON.stringify({
      schema_version: 1,
      release_version: "0.1.0-test",
      files: { "bin/platform": createHash("sha256").update(runtime).digest("hex") },
    }));
    await mkdir(path.join(home, "deployment.json"));
    const result = await run(["deploy", "--home", home, "--runtime-bundle", bundle, "--apply", "--json"]);
    assert.equal(result.code, 1);
    assert.equal(JSON.parse(result.stdout).error_code, "state_write_failed");
    assert.deepEqual((await readdir(home)).filter((name) => name.includes("deployment.json.") && name.endsWith(".tmp")), []);
    assert.deepEqual(await readdir(path.join(home, "runtime", "releases")), []);
  } finally {
    await rm(home, { recursive: true, force: true });
    await rm(bundle, { recursive: true, force: true });
  }
});

test("Platform CLI stages a validated deployment without starting processes", async () => {
  const home = await tempHome();
  const bundle = await mkdtemp(path.join(TEMP_ROOT, "knowledge-platform-bundle-"));
  try {
    await run(["init", "--home", home]);
    await mkdir(path.join(bundle, "bin"), { recursive: true });
    const runtime = "#!/bin/sh\nexit 0\n";
    await writeFile(path.join(bundle, "bin", "platform"), runtime, { mode: 0o700 });
    await writeFile(path.join(bundle, "manifest.json"), JSON.stringify({
      schema_version: 1,
      release_version: "0.1.0-test",
      files: { "bin/platform": createHash("sha256").update(runtime).digest("hex") },
    }));
    const result = await run(["deploy", "--home", home, "--runtime-bundle", bundle, "--apply", "--json"]);
    assert.equal(result.code, 0, result.stdout + result.stderr);
    const deployment = JSON.parse(result.stdout);
    assert.equal(deployment.status, "staged_not_running");
    assert.equal(deployment.processes_started, false);
    assert.equal(deployment.activation_allowed, false);
    assert.equal(deployment.runtime_file_count, 1);
    assert.equal(deployment.runtime_bundle_path, path.join(home, "runtime", "releases", deployment.runtime_manifest_digest.slice("sha256:".length)));
    assert.equal((await stat(path.join(deployment.runtime_bundle_path, "bin", "platform"))).isFile(), true);
    assert.equal((await stat(path.join(deployment.runtime_bundle_path, "manifest.json"))).isFile(), true);
    await assert.rejects(readFile(path.join(home, ".runtime-stage.lock")), { code: "ENOENT" });
    await rm(bundle, { recursive: true, force: true });
    assert.equal((await stat(path.join(deployment.runtime_bundle_path, "bin", "platform"))).isFile(), true);
    const repeated = await run(["deploy", "--home", home, "--runtime-bundle", deployment.runtime_bundle_path, "--apply", "--json"]);
    assert.equal(repeated.code, 0, repeated.stdout + repeated.stderr);
  } finally {
    await rm(home, { recursive: true, force: true });
    await rm(bundle, { recursive: true, force: true });
  }
});

test("Platform runtime verification returns the content address used by owned releases", async () => {
  const bundle = await mkdtemp(path.join(TEMP_ROOT, "knowledge-platform-bundle-verify-"));
  try {
    await mkdir(path.join(bundle, "bin"), { recursive: true });
    const runtime = "runtime\n";
    await writeFile(path.join(bundle, "bin", "platform"), runtime);
    const manifest = {
      schema_version: 1,
      release_version: "0.1.0-test",
      files: { "bin/platform": createHash("sha256").update(runtime).digest("hex") },
    };
    await writeFile(path.join(bundle, "manifest.json"), JSON.stringify(manifest));
    const verified = await verifyRuntimeBundle(bundle);
    assert.deepEqual(verified.manifest, manifest);
    assert.equal(verified.digest, createHash("sha256").update(JSON.stringify(manifest), "utf8").digest("hex"));
  } finally {
    await rm(bundle, { recursive: true, force: true });
  }
});

test("Platform deploy does not overwrite a conflicting content-addressed release", async () => {
  const home = await tempHome();
  const bundle = await mkdtemp(path.join(TEMP_ROOT, "knowledge-platform-bundle-conflict-"));
  try {
    await run(["init", "--home", home]);
    await mkdir(path.join(bundle, "bin"), { recursive: true });
    const runtime = "original\n";
    await writeFile(path.join(bundle, "bin", "platform"), runtime);
    const manifest = {
      schema_version: 1,
      release_version: "0.1.0-test",
      files: { "bin/platform": createHash("sha256").update(runtime).digest("hex") },
    };
    await writeFile(path.join(bundle, "manifest.json"), JSON.stringify(manifest));
    const first = await run(["deploy", "--home", home, "--runtime-bundle", bundle, "--apply", "--json"]);
    assert.equal(first.code, 0, first.stdout + first.stderr);
    const deployment = JSON.parse(first.stdout);
    await writeFile(path.join(deployment.runtime_bundle_path, "bin", "platform"), "tampered\n");
    const second = await run(["deploy", "--home", home, "--runtime-bundle", bundle, "--apply", "--json"]);
    assert.equal(second.code, 1);
    assert.equal(JSON.parse(second.stdout).error_code, "runtime_release_conflict");
    assert.equal(JSON.parse(await readFile(path.join(home, "deployment.json"), "utf8")).runtime_bundle_path, deployment.runtime_bundle_path);
    assert.equal(await readFile(path.join(deployment.runtime_bundle_path, "bin", "platform"), "utf8"), "tampered\n");
  } finally {
    await rm(home, { recursive: true, force: true });
    await rm(bundle, { recursive: true, force: true });
  }
});

test("Platform CLI rejects a bundle whose declared file digest is wrong", async () => {
  const home = await tempHome();
  const bundle = await mkdtemp(path.join(TEMP_ROOT, "knowledge-platform-bundle-invalid-"));
  try {
    await run(["init", "--home", home]);
    await mkdir(path.join(bundle, "bin"), { recursive: true });
    await writeFile(path.join(bundle, "bin", "platform"), "runtime");
    await writeFile(path.join(bundle, "manifest.json"), JSON.stringify({
      schema_version: 1,
      release_version: "0.1.0-test",
      files: { "bin/platform": "0".repeat(64) },
    }));
    const result = await run(["deploy", "--home", home, "--runtime-bundle", bundle, "--apply", "--json"]);
    assert.equal(result.code, 1);
    assert.equal(JSON.parse(result.stdout).error_code, "invalid_runtime_bundle");
    assert.equal(JSON.parse(await readFile(path.join(home, "platform.json"), "utf8")).runtime.status, "not_attached");
  } finally {
    await rm(home, { recursive: true, force: true });
    await rm(bundle, { recursive: true, force: true });
  }
});

test("Platform CLI rejects undeclared runtime files and symlinks", async () => {
  const home = await tempHome();
  const bundle = await mkdtemp(path.join(TEMP_ROOT, "knowledge-platform-bundle-extra-"));
  const target = await mkdtemp(path.join(TEMP_ROOT, "knowledge-platform-bundle-target-"));
  try {
    await run(["init", "--home", home]);
    await mkdir(path.join(bundle, "bin"), { recursive: true });
    const runtime = "#!/bin/sh\nexit 0\n";
    await writeFile(path.join(bundle, "bin", "platform"), runtime, { mode: 0o700 });
    await writeFile(path.join(bundle, "bin", "undeclared"), "unexpected\n");
    await writeFile(path.join(bundle, "manifest.json"), JSON.stringify({
      schema_version: 1,
      release_version: "0.1.0-test",
      files: { "bin/platform": createHash("sha256").update(runtime).digest("hex") },
    }));
    let result = await run(["deploy", "--home", home, "--runtime-bundle", bundle, "--apply", "--json"]);
    assert.equal(result.code, 1);
    assert.equal(JSON.parse(result.stdout).error_code, "invalid_runtime_bundle");
    assert.equal((await readdir(path.join(home, "runtime"))).includes("releases"), false);

    await rm(path.join(bundle, "bin", "undeclared"));
    await symlink(target, path.join(bundle, "linked-dir"));
    result = await run(["deploy", "--home", home, "--runtime-bundle", bundle, "--apply", "--json"]);
    assert.equal(result.code, 1);
    assert.equal(JSON.parse(result.stdout).error_code, "invalid_runtime_bundle");
  } finally {
    await rm(home, { recursive: true, force: true });
    await rm(bundle, { recursive: true, force: true });
    await rm(target, { recursive: true, force: true });
  }
});

test("Platform deploy rejects a symlink occupying an owned release digest", async () => {
  const home = await tempHome();
  const bundle = await mkdtemp(path.join(TEMP_ROOT, "knowledge-platform-bundle-release-link-"));
  const target = await mkdtemp(path.join(TEMP_ROOT, "knowledge-platform-release-target-"));
  try {
    await run(["init", "--home", home]);
    await mkdir(path.join(bundle, "bin"), { recursive: true });
    const runtime = "runtime\n";
    await writeFile(path.join(bundle, "bin", "platform"), runtime);
    const manifest = {
      schema_version: 1,
      release_version: "0.1.0-test",
      files: { "bin/platform": createHash("sha256").update(runtime).digest("hex") },
    };
    await writeFile(path.join(bundle, "manifest.json"), JSON.stringify(manifest));
    const { digest } = await verifyRuntimeBundle(bundle);
    await mkdir(path.join(home, "runtime", "releases"));
    await symlink(target, path.join(home, "runtime", "releases", digest));
    const result = await run(["deploy", "--home", home, "--runtime-bundle", bundle, "--apply", "--json"]);
    assert.equal(result.code, 1);
    assert.equal(JSON.parse(result.stdout).error_code, "runtime_release_conflict");
    assert.equal((await readdir(target)).length, 0);
    await assert.rejects(readFile(path.join(home, "deployment.json")), { code: "ENOENT" });
  } finally {
    await rm(home, { recursive: true, force: true });
    await rm(bundle, { recursive: true, force: true });
    await rm(target, { recursive: true, force: true });
  }
});

test("Platform deploy rejects symlinked bundle and Home ancestors before writing state", async () => {
  const rootHome = await tempHome();
  const actualHome = path.join(rootHome, "home");
  const homeAlias = path.join(rootHome, "home-alias");
  const bundleParent = path.join(rootHome, "bundle-parent");
  const bundleAlias = path.join(rootHome, "bundle-alias");
  const bundle = path.join(bundleParent, "bundle");
  try {
    await run(["init", "--home", actualHome]);
    await mkdir(path.join(bundle, "bin"), { recursive: true });
    const runtime = "runtime\n";
    await writeFile(path.join(bundle, "bin", "platform"), runtime);
    await writeFile(path.join(bundle, "manifest.json"), JSON.stringify({
      schema_version: 1,
      release_version: "0.1.0-test",
      files: { "bin/platform": createHash("sha256").update(runtime).digest("hex") },
    }));
    await symlink(rootHome, homeAlias);
    await symlink(bundleParent, bundleAlias);

    const sourceResult = await run(["deploy", "--home", actualHome, "--runtime-bundle", path.join(bundleAlias, "bundle"), "--apply", "--json"]);
    assert.equal(sourceResult.code, 1);
    assert.equal(JSON.parse(sourceResult.stdout).error_code, "invalid_runtime_bundle");
    const homeResult = await run(["deploy", "--home", path.join(homeAlias, "home"), "--runtime-bundle", bundle, "--apply", "--json"]);
    assert.equal(homeResult.code, 1);
    assert.equal(JSON.parse(homeResult.stdout).error_code, "invalid_home");
    await assert.rejects(readFile(path.join(actualHome, "deployment.json")), { code: "ENOENT" });
  } finally {
    await rm(rootHome, { recursive: true, force: true });
  }
});

test("Platform deploy fails closed while another deployment holds the stage lock", async () => {
  const home = await tempHome();
  const bundle = await mkdtemp(path.join(TEMP_ROOT, "knowledge-platform-bundle-lock-"));
  try {
    await run(["init", "--home", home]);
    await mkdir(path.join(bundle, "bin"), { recursive: true });
    const runtime = "runtime\n";
    await writeFile(path.join(bundle, "bin", "platform"), runtime);
    await writeFile(path.join(bundle, "manifest.json"), JSON.stringify({
      schema_version: 1,
      release_version: "0.1.0-test",
      files: { "bin/platform": createHash("sha256").update(runtime).digest("hex") },
    }));
    await writeFile(path.join(home, ".runtime-stage.lock"), "held\n", { mode: 0o600 });
    const result = await run(["deploy", "--home", home, "--runtime-bundle", bundle, "--apply", "--json"]);
    assert.equal(result.code, 1);
    assert.equal(JSON.parse(result.stdout).error_code, "runtime_stage_locked");
    assert.equal(await readFile(path.join(home, ".runtime-stage.lock"), "utf8"), "held\n");
    await assert.rejects(readFile(path.join(home, "deployment.json")), { code: "ENOENT" });
  } finally {
    await rm(home, { recursive: true, force: true });
    await rm(bundle, { recursive: true, force: true });
  }
});

test("Platform CLI rejects empty and wildcard runtime manifests", async () => {
  assert.throws(() => validateComposeAsset(""), /Platform Compose asset is empty/);
  const { validateRuntimeManifest } = await import("../src/state.mjs");
  assert.throws(() => validateRuntimeManifest({ schema_version: 1, release_version: "x", files: {} }), /runtime manifest files are invalid/);
  assert.throws(() => validateRuntimeManifest({ schema_version: 1, release_version: "x", files: { "bin/*": "0".repeat(64) } }), /concrete bundle path/);
});

test("Platform CLI exposes dry plans for all operation families", async () => {
  const home = await tempHome();
  try {
    for (const kind of ["deploy", "migrate", "import", "export", "index"]) {
      const result = await run(["plan", kind, "--home", home, "--json"]);
      assert.equal(result.code, 0);
      const value = JSON.parse(result.stdout);
      assert.equal(value.kind, kind);
      assert.equal(value.execution_allowed, false);
      assert.deepEqual(value.side_effects, []);
    }
  } finally {
    await rm(home, { recursive: true, force: true });
  }
});

test("Platform CLI exposes independent health, backup, and upgrade lifecycle boundaries", async () => {
  const home = await tempHome();
  try {
    for (const kind of ["health", "backup", "upgrade"]) {
      const result = await run(["plan", kind, "--home", home, "--json"]);
      assert.equal(result.code, 0, result.stdout + result.stderr);
      const plan = JSON.parse(result.stdout);
      assert.equal(plan.format, "agent-knowledge-platform-lifecycle-plan/v1");
      assert.equal(plan.kind, kind);
      assert.equal(plan.owner, "puddingknowledge");
      assert.equal(plan.platform_owned, true);
      assert.equal(plan.execution_allowed, false);
      assert.equal(plan.activation_allowed, false);
      assert.deepEqual(plan.side_effects, []);
      assert.equal(plan.legacy_claw_mutation, false);
      assert.deepEqual(
        plan.required_preconditions,
        kind === "upgrade" ? ["validated_backup_snapshot", "migration_manifest", "rollback_window_open"]
          : kind === "backup" ? ["initialized_platform_home"] : [],
      );
    }
  } finally {
    await rm(home, { recursive: true, force: true });
  }
});

test("Platform health reads only local metadata and backup/upgrade remain staged", async () => {
  const home = await tempHome();
  const outputParent = await tempHome();
  const backup = path.join(outputParent, "upgrade-precondition");
  try {
    await run(["init", "--home", home]);
    const healthResult = await run(["health", "--home", home, "--json"]);
    assert.equal(healthResult.code, 0);
    const health = JSON.parse(healthResult.stdout);
    assert.equal(health.status, "shadow_observed_not_running");
    assert.equal(health.infrastructure.process_started, false);
    assert.equal(health.runtime.process_started, false);
    assert.equal(health.execution_allowed, false);
    assert.match(health.observation_scope, /metadata only/);

    const configPath = path.join(home, "platform.json");
    const config = JSON.parse(await readFile(configPath, "utf8"));
    config.infrastructure.process_started = true;
    await writeFile(configPath, `${JSON.stringify(config)}\n`);
    const claimedStarted = JSON.parse((await run(["health", "--home", home, "--json"])).stdout);
    assert.equal(claimedStarted.status, "shadow_metadata_claims_started_unprobed");
    assert.equal(claimedStarted.infrastructure.process_started, true);

    const backupResult = await run(["backup", "--home", home, "--output", backup, "--apply", "--json"]);
    assert.equal(backupResult.code, 0, backupResult.stdout + backupResult.stderr);
    const migrationManifest = path.join(outputParent, "migration.json");
    await writeFile(migrationManifest, JSON.stringify({
      format: "agent-knowledge-platform-installation-migration/v1",
      source: { installation_id: "local-shadow", schema_revision: "schema-v1", catalog_revision: "catalog-v1" },
      targets: { puddingknowledge: "v1", puddingharness: "v1" },
      object_summaries: [],
      id_resource_mappings: [],
      credential_rebinds: [],
      active_writers: { session_harness: "puddingclaw", knowledge_catalog: "puddingclaw", connector_jobs: "puddingclaw" },
      checkpoint: { target_import: "staged" },
      rollback_strategy: "snapshot_restore",
      state: "PREPARED",
      snapshot_digest: JSON.parse(backupResult.stdout).manifest_digest,
      staging_namespace: "local-staging",
      active_installation_revision: null,
      rollback_window_open: true,
    }) + "\n");

    for (const kind of ["backup", "upgrade"]) {
      const argumentsForKind = [kind, "--home", home, "--apply"];
      if (kind === "upgrade") argumentsForKind.push("--backup", backup, "--migration-manifest", migrationManifest);
      argumentsForKind.push("--json");
      const result = await run(argumentsForKind);
      assert.equal(result.code, 0, result.stdout + result.stderr);
      const staged = JSON.parse(result.stdout);
      assert.equal(staged.status, "staged_not_executed");
      assert.equal(staged.execution_allowed, false);
      assert.equal(staged.activation_allowed, false);
      assert.equal(staged.lifecycle.kind, kind);
      assert.deepEqual(staged.lifecycle.side_effects, []);
      if (kind === "upgrade") {
        assert.equal(staged.lifecycle.backup_manifest_digest, JSON.parse(backupResult.stdout).manifest_digest);
        assert.match(staged.lifecycle.migration_manifest_digest, /^sha256:[0-9a-f]{64}$/);
        assert.equal(staged.lifecycle.migration_state, "PREPARED");
        assert.equal(staged.lifecycle.rollback_window, "open");
      }
    }
  } finally {
    await rm(home, { recursive: true, force: true });
    await rm(outputParent, { recursive: true, force: true });
  }
});

test("Platform upgrade refuses to stage without a validated backup", async () => {
  const home = await tempHome();
  try {
    await run(["init", "--home", home]);
    const missing = await run(["upgrade", "--home", home, "--apply", "--json"]);
    assert.equal(missing.code, 1);
    assert.equal(JSON.parse(missing.stdout).error_code, "backup_required");
  } finally {
    await rm(home, { recursive: true, force: true });
  }
});

test("Platform backup creates and validates an atomic content-addressed snapshot", async () => {
  const home = await tempHome();
  const outputParent = await tempHome();
  const backup = path.join(outputParent, "snapshot-v1");
  try {
    await run(["init", "--home", home]);
    await writeFile(path.join(home, "catalog", "catalog.sqlite3"), "catalog-state\n");
    await writeFile(path.join(home, "packages", "package-manifest.json"), "{\"revision\":\"v1\"}\n");
    await writeFile(path.join(home, "runtime", "platform.bin"), "runtime-state\n");
    await writeFile(path.join(home, "infrastructure", "state.db"), "infra-state\n");
    await writeFile(path.join(home, "logs", "excluded.log"), "password=must-not-be-copied\n");
    await writeFile(path.join(home, "operations", "excluded.json"), "token=must-not-be-copied\n");

    const result = await run(["backup", "--home", home, "--output", backup, "--apply", "--json"]);
    assert.equal(result.code, 0, result.stdout + result.stderr);
    const snapshot = JSON.parse(result.stdout);
    assert.equal(snapshot.status, "snapshot_created");
    assert.equal(snapshot.validation, "valid");
    assert.equal(snapshot.format, "agent-knowledge-platform-backup/v1");
    assert.equal(snapshot.activation_allowed, false);
    assert.equal(snapshot.execution_allowed, false);
    assert.equal(snapshot.secret_bytes_copied, false);
    assert.deepEqual(snapshot.excluded_roots, ["logs", "operations"]);
    assert.deepEqual(Object.keys(snapshot.files).sort(), [
      "catalog/catalog.sqlite3",
      "infrastructure/state.db",
      "packages/package-manifest.json",
      "platform.json",
      "runtime/platform.bin",
    ]);
    assert.equal(snapshot.file_count, 5);
    assert.equal((await stat(path.join(backup, "manifest.json"))).isFile(), true);
    await assert.rejects(stat(path.join(backup, "logs", "excluded.log")), { code: "ENOENT" });
    const missingMigration = await run(["upgrade", "--home", home, "--backup", backup, "--apply", "--json"]);
    assert.equal(missingMigration.code, 1);
    assert.equal(JSON.parse(missingMigration.stdout).error_code, "migration_manifest_required");

    const invalidMigration = path.join(outputParent, "invalid-migration.json");
    await writeFile(invalidMigration, JSON.stringify({
      format: "agent-knowledge-platform-installation-migration/v1",
      source: "not-an-object",
      targets: { puddingknowledge: "v1" },
      object_summaries: [],
      id_resource_mappings: [],
      credential_rebinds: [],
      active_writers: { session_harness: "puddingclaw", knowledge_catalog: "puddingclaw", connector_jobs: "puddingclaw" },
      checkpoint: { target_import: "staged" },
      rollback_strategy: "snapshot_restore",
      state: "PREPARED",
      snapshot_digest: snapshot.manifest_digest,
      staging_namespace: "local-staging",
      active_installation_revision: null,
      rollback_window_open: true,
    }) + "\n");
    const rejectedMigration = await run(["upgrade", "--home", home, "--backup", backup, "--migration-manifest", invalidMigration, "--apply", "--json"]);
    assert.equal(rejectedMigration.code, 1);
    assert.equal(JSON.parse(rejectedMigration.stdout).error_code, "invalid_migration_manifest");

    const validated = await run(["backup", "--validate", "--output", backup, "--json"]);
    assert.equal(validated.code, 0, validated.stdout + validated.stderr);
    assert.equal(JSON.parse(validated.stdout).status, "valid");
    assert.equal(JSON.parse(validated.stdout).manifest_digest, snapshot.manifest_digest);

    const restoredHome = path.join(outputParent, "restored-home");
    const restorePlan = await run(["restore", "--backup", backup, "--target", restoredHome, "--json"]);
    assert.equal(restorePlan.code, 0, restorePlan.stdout + restorePlan.stderr);
    const plan = JSON.parse(restorePlan.stdout);
    assert.equal(plan.status, "validated_not_executed");
    assert.equal(plan.source_manifest_digest, snapshot.manifest_digest);
    assert.equal(plan.execution_allowed, false);
    assert.equal(plan.activation_allowed, false);
    assert.deepEqual(plan.side_effects, []);
    assert.equal((await stat(restoredHome).catch(() => null)), null);

    const restored = await run(["restore", "--backup", backup, "--target", restoredHome, "--apply", "--json"]);
    assert.equal(restored.code, 0, restored.stdout + restored.stderr);
    const restoreResult = JSON.parse(restored.stdout);
    assert.equal(restoreResult.status, "restored_local_not_running");
    assert.equal(restoreResult.format, "agent-knowledge-platform-restore/v1");
    assert.equal(restoreResult.source_manifest_digest, snapshot.manifest_digest);
    assert.equal(restoreResult.execution_allowed, true);
    assert.equal(restoreResult.activation_allowed, false);
    assert.equal(restoreResult.production_endpoint, false);
    assert.equal((await stat(path.join(restoredHome, "catalog", "catalog.sqlite3"))).isFile(), true);
    assert.equal((await stat(path.join(restoredHome, "logs"))).isDirectory(), true);
    await assert.rejects(stat(path.join(restoredHome, "logs", "excluded.log")), { code: "ENOENT" });
    const repeatRestore = await run(["restore", "--backup", backup, "--target", restoredHome, "--apply", "--json"]);
    assert.equal(repeatRestore.code, 1);
    assert.equal(JSON.parse(repeatRestore.stdout).error_code, "restore_target_exists");
    const invalidRestoreArgs = await run(["restore", "--backup", backup, "--target", path.join(outputParent, "another-home"), "--output", path.join(outputParent, "ignored"), "--json"]);
    assert.equal(invalidRestoreArgs.code, 1);
    assert.equal(JSON.parse(invalidRestoreArgs.stdout).error_code, "argument_error");

    await writeFile(path.join(backup, "unexpected.txt"), "tampered\n");
    const rejected = await run(["backup", "--validate", "--output", backup, "--json"]);
    assert.equal(rejected.code, 1);
    assert.equal(JSON.parse(rejected.stdout).error_code, "invalid_backup");
  } finally {
    await rm(home, { recursive: true, force: true });
    await rm(outputParent, { recursive: true, force: true });
  }
});

test("Platform backup rejects symlinks and secret-bearing files before publishing a target", async () => {
  const home = await tempHome();
  const outputParent = await tempHome();
  const backup = path.join(outputParent, "snapshot");
  const target = await tempHome();
  const linkedHome = path.join(outputParent, "linked-home");
  try {
    await run(["init", "--home", home]);
    await symlink(target, path.join(home, "runtime", "linked-runtime"));
    let result = await run(["backup", "--home", home, "--output", backup, "--apply", "--json"]);
    assert.equal(result.code, 1);
    assert.equal(JSON.parse(result.stdout).error_code, "invalid_backup");
    await assert.rejects(stat(backup), { code: "ENOENT" });

    await rm(path.join(home, "runtime", "linked-runtime"));
    await writeFile(path.join(home, "packages", "secret.txt"), "api_key=do-not-copy\n");
    result = await run(["backup", "--home", home, "--output", backup, "--apply", "--json"]);
    assert.equal(result.code, 1);
    assert.equal(JSON.parse(result.stdout).error_code, "secret_bearing_backup");
    await assert.rejects(stat(backup), { code: "ENOENT" });

    await rm(path.join(home, "packages", "secret.txt"));
    await symlink(home, linkedHome);
    result = await run(["backup", "--home", linkedHome, "--output", backup, "--apply", "--json"]);
    assert.equal(result.code, 1);
    assert.equal(JSON.parse(result.stdout).error_code, "invalid_home");
    await assert.rejects(stat(backup), { code: "ENOENT" });
  } finally {
    await rm(home, { recursive: true, force: true });
    await rm(outputParent, { recursive: true, force: true });
    await rm(target, { recursive: true, force: true });
  }
});

test("Platform lifecycle commands reject unrelated data path arguments", async () => {
  const home = await tempHome();
  try {
    await run(["init", "--home", home]);
    for (const kind of ["backup", "upgrade"]) {
      const result = await run([kind, "--home", home, "--apply", "--source", "/tmp/source", "--json"]);
      assert.equal(result.code, 1);
      assert.equal(JSON.parse(result.stdout).error_code, "argument_error");
    }
  } finally {
    await rm(home, { recursive: true, force: true });
  }
});

test("Platform CLI exposes an explicit Platform-owned infrastructure plan and staging operation", async () => {
  const home = await tempHome();
  try {
    const planResult = await run(["plan", "infrastructure", "--home", home, "--json"]);
    assert.equal(planResult.code, 0, planResult.stdout + planResult.stderr);
    const plan = JSON.parse(planResult.stdout);
    assert.equal(plan.owner, "puddingknowledge");
    assert.deepEqual(plan.legacy_dependencies, []);
    assert.deepEqual(plan.services, ["postgres", "milvus-etcd", "milvus-minio", "milvus", "api", "worker", "console"]);
    assert.equal(plan.execution_allowed, false);
    assert.equal(plan.process_started, false);
    assert.deepEqual(plan.side_effects, []);
    assert.match(plan.compose.digest, /^sha256:[0-9a-f]{64}$/);
    assert.deepEqual(plan.compose.services, ["postgres", "milvus-etcd", "milvus-minio", "milvus", "api", "worker", "console"]);

    await run(["init", "--home", home]);
    const stagedResult = await run(["infrastructure", "--home", home, "--apply", "--json"]);
    assert.equal(stagedResult.code, 0, stagedResult.stdout + stagedResult.stderr);
    const staged = JSON.parse(stagedResult.stdout);
    assert.equal(staged.kind, "infrastructure");
    assert.equal(staged.status, "staged_not_executed");
    assert.equal(staged.execution_allowed, false);
    assert.equal(staged.details.process_started, false);
    assert.equal(staged.details.compose_digest, plan.compose.digest);
  } finally {
    await rm(home, { recursive: true, force: true });
  }
});

test("Platform Compose validator rejects legacy references, missing required secrets, and wrong services", async () => {
  const compose = await readFile(new URL("../assets/compose.platform.yml", import.meta.url), "utf8");
  const valid = validateComposeAsset(compose);
  assert.match(valid.digest, /^sha256:[0-9a-f]{64}$/);
  assert.throws(() => validateComposeAsset(compose.replace("puddingknowledge-api", "puddingclaw-api")), /legacy Claw/);
  assert.throws(
    () => validateComposeAsset(compose.replace("PUDDINGKNOWLEDGE_API_IMAGE:?set PUDDINGKNOWLEDGE_API_IMAGE", "PUDDINGKNOWLEDGE_API_IMAGE:latest")),
    /secret input is not required/,
  );
  assert.throws(
    () => validateComposeAsset(`${compose}\n      OTHER_PASSWORD: plaintext\n`),
    /plaintext password/,
  );
  assert.throws(() => validateComposeAsset(compose.replace("  worker:\n", "  legacy-worker:\n")), /service set is invalid/);
});

test("Platform CLI rejects a symlink Platform Home", async () => {
  const target = await tempHome();
  const parent = await tempHome();
  const link = path.join(parent, "platform-home");
  try {
    await symlink(target, link);
    const result = await run(["init", "--home", link, "--json"]);
    assert.equal(result.code, 1);
    assert.equal(JSON.parse(result.stdout).error_code, "invalid_home");
    await assert.rejects(stat(path.join(target, "platform.json")), { code: "ENOENT" });
  } finally {
    await rm(parent, { recursive: true, force: true });
    await rm(target, { recursive: true, force: true });
  }
});

test("Platform CLI rechecks managed directories on repeated init", async () => {
  const home = await tempHome();
  const target = await tempHome();
  try {
    const initialized = await run(["init", "--home", home, "--json"]);
    assert.equal(initialized.code, 0);
    await rm(path.join(home, "runtime"), { recursive: true, force: true });
    await symlink(target, path.join(home, "runtime"));
    const result = await run(["init", "--home", home, "--json"]);
    assert.equal(result.code, 1);
    assert.equal(JSON.parse(result.stdout).error_code, "invalid_home");
  } finally {
    await rm(home, { recursive: true, force: true });
    await rm(target, { recursive: true, force: true });
  }
});
