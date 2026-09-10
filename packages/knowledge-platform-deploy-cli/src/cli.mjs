#!/usr/bin/env node

import os from "node:os";
import { createHash } from "node:crypto";
import { readFile, lstat } from "node:fs/promises";
import path from "node:path";
import { createRequire } from "node:module";
import {
  PlatformCliError,
  initialize,
  validateComposeAsset,
  loadConfig,
  infrastructurePlan,
  stageDeployment,
  stageInfrastructure,
  stageOperation,
  status,
  health,
  lifecyclePlan,
  PLATFORM_LIFECYCLE_OPERATIONS,
  backupPlatformHome,
  validateBackupDirectory,
  restorePlan,
  restorePlatformHome,
  validateMigrationManifest,
} from "./state.mjs";

import { installRuntime, runtimeCommand } from "./runtime-install.mjs";

const { version } = createRequire(import.meta.url)("../package.json");
const DEFAULT_HOME = path.join(os.homedir(), ".puddingknowledge");

function parseArgs(argv) {
  const positionals = [];
  const flags = {};
  for (let index = 0; index < argv.length; index += 1) {
    const token = argv[index];
    if (!token.startsWith("--")) {
      positionals.push(token);
      continue;
    }
    const [rawName, inline] = token.slice(2).split("=", 2);
    const name = rawName.replaceAll("-", "_");
    if (name === "json" || name === "apply" || name === "force" || name === "help" || name === "validate") {
      flags[name] = true;
      continue;
    }
    const value = inline ?? argv[++index];
    if (value === undefined || value.startsWith("--")) throw new PlatformCliError(`missing value for --${rawName}`, { code: "argument_error" });
    if (!["home", "runtime_bundle", "package", "source", "target", "output", "backup", "migration_manifest", "catalog", "wiki_root", "port", "database_config", "structured_config", "state_dir", "wiki_config", "capture_config", "feishu_config", "file_config"].includes(name)) {
      throw new PlatformCliError(`unknown option: --${rawName}`, { code: "argument_error" });
    }
    flags[name] = value;
  }
  return { positionals, flags };
}

function usage() {
  return [
    "Knowledge Platform CLI",
    "",
    "  knowledge-platform install [--home <absolute-path>] [--apply] [--json]",
    "  knowledge-platform start --catalog <absolute-path> --wiki-root <absolute-path> --port <port> [--file-config <absolute-path>] [--apply] [--json]",
    "  knowledge-platform stop [--home <absolute-path>] [--apply] [--json]",
    "  knowledge-platform init [--home <absolute-path>] [--force] [--json]",
    "  knowledge-platform status [--home <absolute-path>] [--json]",
    "  knowledge-platform plan <deploy|infrastructure|health|backup|upgrade|migrate|import|export|index> [--home <absolute-path>] [--json]",
    "  knowledge-platform health [--home <absolute-path>] [--json]",
    "  knowledge-platform deploy --runtime-bundle <absolute-path> [--apply] [--json]",
    "  knowledge-platform infrastructure|backup|upgrade|migrate|import|export|index [--apply] [--json]",
    "  knowledge-platform backup --validate --output <absolute-path> [--json]",
    "  knowledge-platform restore --backup <absolute-path> --target <absolute-path> [--apply] [--json]",
  ].join("\n");
}

function output(value, json) {
  if (json) process.stdout.write(`${JSON.stringify(value, null, 2)}\n`);
  else process.stdout.write(`${typeof value === "string" ? value : JSON.stringify(value, null, 2)}\n`);
}

function plan(kind, home) {
  return {
    schema_version: 1,
    kind,
    status: "plan",
    home_digest: `sha256:${createHash("sha256").update(home, "utf8").digest("hex")}`,
    execution_allowed: false,
    activation_allowed: false,
    side_effects: [],
    next_boundary: "platform_worker_not_attached",
  };
}

async function main(argv) {
  const { positionals, flags } = parseArgs(argv);
  if (flags.help || !positionals[0]) return { value: usage(), code: 0 };
  const command = positionals[0];
  const requestedHome = flags.home || process.env.PUDDINGKNOWLEDGE_HOME || DEFAULT_HOME;
  if (!path.isAbsolute(requestedHome)) throw new PlatformCliError("Platform Home must be absolute", { code: "invalid_home" });
  const home = path.resolve(requestedHome);
  if (command === "version") return { value: { schema_version: 1, cli: "knowledge-platform", cli_version: version }, code: 0 };
  if (command === "init") return { value: await initialize(home, { force: Boolean(flags.force) }), code: 0 };
  if (["install", "start", "stop"].includes(command)) {
    if (!(await loadConfig(home))) throw new PlatformCliError("Platform is not initialized", { code: "not_initialized" });
    if (!flags.apply) return { value: plan(command, home), code: 0 };
    return { value: command === "install" ? await installRuntime(home) : await runtimeCommand(command, home, flags), code: 0 };
  }
  if (command === "status" || command === "health") {
    const metadata = command === "status" ? await status(home) : await health(home);
    let installed = false;
    try { await lstat(path.join(home, "runtime", "installed.json")); installed = true; }
    catch (e) { if (e.code !== "ENOENT") throw e; }
    if (!installed) return { value: metadata, code: 0 };
    const observed = await runtimeCommand("status", home);
    return { value: { ...metadata, status: observed.status, runtime_observation: observed },
      code: command === "health" && observed.status !== "running" ? 1 : 0 };
  }
  if (command === "backup" && flags.validate) {
    if (flags.apply || flags.home || flags.backup || flags.target || flags.migration_manifest || flags.force || typeof flags.output !== "string") {
      throw new PlatformCliError("backup --validate requires --output and cannot use --apply", { code: "argument_error" });
    }
    return { value: await validateBackupDirectory(flags.output), code: 0 };
  }
  if (command === "restore") {
    if (typeof flags.backup !== "string" || typeof flags.target !== "string" || flags.home
      || flags.output || flags.runtime_bundle || flags.package || flags.source || flags.migration_manifest
      || flags.force || flags.validate) {
      throw new PlatformCliError("restore requires --backup and --target and does not accept --home", { code: "argument_error" });
    }
    if (flags.apply) return { value: await restorePlatformHome(flags.backup, flags.target), code: 0 };
    return { value: await restorePlan(flags.backup, flags.target), code: 0 };
  }
  if (command === "plan") {
    const kind = positionals[1];
    if (kind === "infrastructure") {
      const composeText = await readFile(new URL("../assets/compose.platform.yml", import.meta.url), "utf8");
      const compose = validateComposeAsset(composeText);
      return { value: { ...infrastructurePlan(home), compose }, code: 0 };
    }
    if (PLATFORM_LIFECYCLE_OPERATIONS.includes(kind)) return { value: lifecyclePlan(kind, home), code: 0 };
    if (!["deploy", "migrate", "import", "export", "index"].includes(kind)) throw new PlatformCliError("plan kind is invalid", { code: "argument_error" });
    return { value: plan(kind, home), code: 0 };
  }
  if (!["deploy", "infrastructure", ...PLATFORM_LIFECYCLE_OPERATIONS.filter((kind) => kind !== "health"), "migrate", "import", "export", "index"].includes(command)) throw new PlatformCliError(`unknown command: ${command}`, { code: "argument_error" });
  if (!(await loadConfig(home))) throw new PlatformCliError("Platform is not initialized; run knowledge-platform init", { code: "not_initialized" });
  if (!flags.apply) return { value: plan(command, home), code: 0 };
  if (command === "deploy") {
    const value = await stageDeployment(home, flags.runtime_bundle);
    return { value, code: 0 };
  }
  if (command === "infrastructure") {
    let composeText;
    try {
      composeText = await readFile(new URL("../assets/compose.platform.yml", import.meta.url), "utf8");
    } catch {
      throw new PlatformCliError("Platform Compose asset cannot be read", { code: "invalid_infrastructure_asset" });
    }
    return { value: await stageInfrastructure(home, composeText), code: 0 };
  }
  if (command === "backup" || command === "upgrade") {
    if (flags.package || flags.source || flags.target || flags.runtime_bundle || (command === "backup" && (flags.backup || flags.migration_manifest))) {
      throw new PlatformCliError(`${command} does not accept unrelated data path arguments`, { code: "argument_error" });
    }
    if (command === "upgrade" && flags.output) {
      throw new PlatformCliError("upgrade does not accept --output; use a migration runner", { code: "argument_error" });
    }
    if (command === "backup" && flags.output) {
      const value = await backupPlatformHome(home, flags.output);
      return { value, code: 0 };
    }
    if (command === "upgrade") {
      if (typeof flags.backup !== "string") {
        throw new PlatformCliError("upgrade requires --backup pointing to a validated snapshot", { code: "backup_required" });
      }
      if (typeof flags.migration_manifest !== "string") {
        throw new PlatformCliError("upgrade requires --migration-manifest in PREPARED state", { code: "migration_manifest_required" });
      }
      const backup = await validateBackupDirectory(flags.backup);
      const migration = await validateMigrationManifest(flags.migration_manifest, backup.manifest_digest);
      const lifecycle = {
        ...lifecyclePlan(command, home),
        backup_manifest_digest: backup.manifest_digest,
        backup_file_count: backup.file_count,
        migration_manifest_digest: migration.manifest_digest,
        migration_state: migration.state,
        rollback_window: "open",
      };
      const operation = await stageOperation(home, command, lifecycle);
      return { value: { ...operation, lifecycle }, code: 0 };
    }
    const lifecycle = lifecyclePlan(command, home);
    const operation = await stageOperation(home, command, lifecycle);
    return { value: { ...operation, lifecycle }, code: 0 };
  }
  const value = await stageOperation(home, command, {
    package_location_digest: typeof flags.package === "string"
      ? `sha256:${createHash("sha256").update(flags.package, "utf8").digest("hex")}`
      : null,
    source_kind: flags.source ? "explicit_local_source" : null,
    target_kind: flags.target ? "explicit_local_target" : null,
  });
  return { value, code: 0 };
}

try {
  const { flags } = parseArgs(process.argv.slice(2));
  const result = await main(process.argv.slice(2));
  output(result.value, Boolean(flags.json));
  process.exitCode = result.code;
} catch (error) {
  const cliError = error instanceof PlatformCliError ? error : new PlatformCliError("internal error", { code: "internal_error" });
  const json = process.argv.includes("--json");
  const value = { schema_version: 1, status: "error", error_code: cliError.code, error: cliError.message };
  output(json ? value : cliError.message, json);
  process.exitCode = cliError.exitCode;
}
