import { createHash, randomUUID } from "node:crypto";
import { chmod, lstat, mkdir, readFile, readdir, realpath, rename, rm, writeFile } from "node:fs/promises";
import path from "node:path";

export const CONFIG_FILE = "platform.json";
export const DEPLOYMENT_FILE = "deployment.json";
export const OPERATIONS_DIR = "operations";
export const PLATFORM_INFRASTRUCTURE_SERVICES = Object.freeze([
  "postgres",
  "milvus-etcd",
  "milvus-minio",
  "milvus",
  "api",
  "worker",
  "console",
]);

export const PLATFORM_LIFECYCLE_OPERATIONS = Object.freeze(["health", "backup", "upgrade"]);
export const PLATFORM_BACKUP_ROOTS = Object.freeze([
  "platform.json",
  "deployment.json",
  "catalog",
  "packages",
  "runtime",
  "infrastructure",
]);
export const PLATFORM_BACKUP_EXCLUSIONS = Object.freeze(["logs", "operations"]);

const RELATIVE_RE = /^(?!\/)(?!.*(?:^|\/)\.\.(?:\/|$))[A-Za-z0-9._*/-]+$/;
const MANAGED_DIRECTORIES = Object.freeze([
  "catalog",
  "packages",
  "artifacts",
  "runtime",
  "logs",
  "infrastructure/postgres",
  "infrastructure/milvus/etcd",
  "infrastructure/milvus/minio",
  "infrastructure/milvus/data",
  OPERATIONS_DIR,
]);
const REQUIRED_COMPOSE_SERVICES = Object.freeze([
  "postgres",
  "milvus-etcd",
  "milvus-minio",
  "milvus",
  "api",
  "worker",
  "console",
]);
const REQUIRED_COMPOSE_SECRET_INPUTS = Object.freeze([
  "PUDDINGKNOWLEDGE_POSTGRES_USER",
  "PUDDINGKNOWLEDGE_POSTGRES_PASSWORD",
  "PUDDINGKNOWLEDGE_MINIO_ROOT_USER",
  "PUDDINGKNOWLEDGE_MINIO_ROOT_PASSWORD",
  "PUDDINGKNOWLEDGE_API_IMAGE",
  "PUDDINGKNOWLEDGE_WORKER_IMAGE",
  "PUDDINGKNOWLEDGE_CONSOLE_IMAGE",
]);
const RUNTIME_STAGE_LOCK = ".runtime-stage.lock";

export class PlatformCliError extends Error {
  constructor(message, { code = "platform_cli_error", exitCode = 1 } = {}) {
    super(message);
    this.name = "PlatformCliError";
    this.code = code;
    this.exitCode = exitCode;
  }
}

function assertPlainObject(value, label) {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new PlatformCliError(`${label} is invalid`, { code: "invalid_state" });
  }
  return value;
}

function assertRelative(value, label) {
  if (typeof value !== "string" || !RELATIVE_RE.test(value) || value.endsWith("/") || value.includes("//")) {
    throw new PlatformCliError(`${label} is invalid`, { code: "invalid_path" });
  }
  return value;
}

async function assertNoSymlinkAncestors(target, label, code, { allowMissing = false } = {}) {
  if (typeof target !== "string" || !path.isAbsolute(target)) {
    throw new PlatformCliError(`${label} must be an absolute path`, { code });
  }
  let current = path.parse(target).root;
  for (const component of target.slice(current.length).split(path.sep).filter(Boolean)) {
    current = path.join(current, component);
    let metadata;
    try {
      metadata = await lstat(current);
    } catch (error) {
      if (error?.code === "ENOENT") {
        if (allowMissing) return;
        throw new PlatformCliError(`${label} cannot be inspected`, { code });
      }
      throw new PlatformCliError(`${label} cannot be inspected`, { code });
    }
    if (metadata.isSymbolicLink()) {
      throw new PlatformCliError(`${label} contains a symlink`, { code });
    }
  }
}

async function assertExistingHomeDirectory(home) {
  await assertNoSymlinkAncestors(home, "Platform Home", "invalid_home");
  const metadata = await lstat(home).catch(() => null);
  if (!metadata || metadata.isSymbolicLink() || !metadata.isDirectory()) {
    throw new PlatformCliError("Platform Home must be a real directory", { code: "invalid_home" });
  }
}

async function acquireRuntimeStageLock(home) {
  const lockPath = path.join(home, RUNTIME_STAGE_LOCK);
  try {
    await writeFile(lockPath, `${randomUUID()}\n`, { encoding: "utf8", mode: 0o600, flag: "wx" });
  } catch (error) {
    if (error?.code === "EEXIST") {
      throw new PlatformCliError("another runtime deployment is already staging", { code: "runtime_stage_locked" });
    }
    throw new PlatformCliError("runtime deployment lock cannot be acquired", { code: "runtime_stage_lock_unavailable" });
  }
  return lockPath;
}

async function bundleFiles(root, prefix = "") {
  const entries = await readdir(root, { withFileTypes: true });
  const files = [];
  for (const entry of entries) {
    const relative = prefix ? `${prefix}/${entry.name}` : entry.name;
    if (relative === "manifest.json") continue;
    const candidate = path.join(root, entry.name);
    const metadata = await lstat(candidate);
    if (metadata.isSymbolicLink()) {
      throw new PlatformCliError("runtime bundle cannot contain symlinks", { code: "invalid_runtime_bundle" });
    }
    if (metadata.isDirectory()) {
      files.push(...await bundleFiles(candidate, relative));
    } else if (metadata.isFile()) {
      files.push(relative);
    } else {
      throw new PlatformCliError("runtime bundle contains an unsupported file type", { code: "invalid_runtime_bundle" });
    }
  }
  return files;
}

export async function ensureHome(home) {
  if (typeof home !== "string" || !path.isAbsolute(home)) {
    throw new PlatformCliError("Platform Home must be an absolute path", { code: "invalid_home" });
  }
  await assertNoSymlinkAncestors(path.dirname(home), "Platform Home", "invalid_home", { allowMissing: true });
  try {
    const metadata = await lstat(home);
    if (metadata.isSymbolicLink() || !metadata.isDirectory()) {
      throw new PlatformCliError("Platform Home must be a real directory", { code: "invalid_home" });
    }
    return home;
  } catch (error) {
    if (error instanceof PlatformCliError) throw error;
    if (error?.code !== "ENOENT") {
      throw new PlatformCliError("Platform Home cannot be inspected", { code: "home_unavailable" });
    }
  }
  try {
    await mkdir(home, { recursive: true, mode: 0o700 });
  } catch (error) {
    throw new PlatformCliError("Platform Home cannot be created", { code: "home_unavailable" });
  }
  return home;
}

async function readJson(file, fallback = null) {
  try {
    return assertPlainObject(JSON.parse(await readFile(file, "utf8")), path.basename(file));
  } catch (error) {
    if (error?.code === "ENOENT") return fallback;
    if (error instanceof PlatformCliError) throw error;
    throw new PlatformCliError(`${path.basename(file)} is not valid JSON`, { code: "invalid_state" });
  }
}

async function writeJsonAtomic(file, value) {
  await mkdir(path.dirname(file), { recursive: true, mode: 0o700 });
  const temporary = `${file}.${randomUUID()}.tmp`;
  try {
    await writeFile(temporary, `${JSON.stringify(value, null, 2)}\n`, { encoding: "utf8", mode: 0o600 });
    await rename(temporary, file);
  } catch (error) {
    throw new PlatformCliError(`${path.basename(file)} cannot be written`, { code: "state_write_failed" });
  } finally {
    await rm(temporary, { force: true }).catch(() => {});
  }
}

async function ensureManagedDirectories(home) {
  for (const directory of MANAGED_DIRECTORIES) {
    let current = home;
    for (const component of directory.split("/")) {
      current = path.join(current, component);
      let metadata;
      try {
        metadata = await lstat(current);
      } catch (error) {
        if (error?.code !== "ENOENT") {
          throw new PlatformCliError("Platform Home cannot be inspected", { code: "home_unavailable" });
        }
      }
      if (metadata) {
        if (metadata.isSymbolicLink() || !metadata.isDirectory()) {
          throw new PlatformCliError("Platform Home contains an unsafe directory", { code: "invalid_home" });
        }
      } else {
        await mkdir(current, { mode: 0o700 });
      }
    }
  }
}

function assertBackupRelative(value, label = "backup path") {
  if (
    typeof value !== "string"
    || !value
    || path.isAbsolute(value)
    || value.includes("\\")
    || value.split("/").includes("..")
    || value.split("/").includes("")
    || [...value].some((character) => {
      const code = character.codePointAt(0);
      return code !== undefined && code < 32;
    })
  ) {
    throw new PlatformCliError(`${label} is invalid`, { code: "invalid_backup" });
  }
  return value;
}

async function collectBackupFiles(root, relativeRoot, files = []) {
  const metadata = await lstat(root);
  if (metadata.isSymbolicLink()) {
    throw new PlatformCliError("Platform backup cannot contain symlinks", { code: "invalid_backup" });
  }
  if (metadata.isDirectory()) {
    const entries = (await readdir(root, { withFileTypes: true })).sort((left, right) => left.name.localeCompare(right.name));
    for (const entry of entries) {
      const childRelative = relativeRoot ? `${relativeRoot}/${entry.name}` : entry.name;
      await collectBackupFiles(path.join(root, entry.name), childRelative, files);
    }
    return files;
  }
  if (!metadata.isFile()) {
    throw new PlatformCliError("Platform backup contains an unsupported file type", { code: "invalid_backup" });
  }
  files.push({ path: assertBackupRelative(relativeRoot), source: root });
  return files;
}

async function ensureBackupTarget(home, outputDir) {
  if (typeof outputDir !== "string" || !path.isAbsolute(outputDir)) {
    throw new PlatformCliError("backup output must be an absolute path", { code: "invalid_backup" });
  }
  const sourceRoot = await realpath(home).catch(() => {
    throw new PlatformCliError("Platform Home cannot be resolved", { code: "invalid_home" });
  });
  const target = path.resolve(outputDir);
  const relative = path.relative(sourceRoot, target);
  if (!relative || (!relative.startsWith(`..${path.sep}`) && relative !== ".." && !path.isAbsolute(relative))) {
    throw new PlatformCliError("backup output must be outside Platform Home", { code: "invalid_backup" });
  }
  const parent = path.dirname(target);
  let parentMetadata;
  try {
    parentMetadata = await lstat(parent);
  } catch (error) {
    if (error?.code === "ENOENT") {
      throw new PlatformCliError("backup output parent must already exist", { code: "invalid_backup" });
    }
    throw new PlatformCliError("backup output parent cannot be inspected", { code: "invalid_backup" });
  }
  if (parentMetadata.isSymbolicLink() || !parentMetadata.isDirectory()) {
    throw new PlatformCliError("backup output parent must be a real directory", { code: "invalid_backup" });
  }
  try {
    const targetMetadata = await lstat(target);
    if (targetMetadata.isSymbolicLink() || targetMetadata.isDirectory() || targetMetadata.isFile()) {
      throw new PlatformCliError("backup output already exists", { code: "backup_target_exists" });
    }
  } catch (error) {
    if (error instanceof PlatformCliError) throw error;
    if (error?.code !== "ENOENT") throw new PlatformCliError("backup output cannot be inspected", { code: "invalid_backup" });
  }
  return target;
}

const SECRET_MARKER_RE = /(?:password|secret|token|authorization|api[_ -]?key|private[_ -]?key)\s*[:=]\s*[^\s,;]+/i;

function backupManifestProjection(manifest) {
  const { manifest_digest: _manifestDigest, ...projection } = manifest;
  return projection;
}

function digestJson(value) {
  return `sha256:${createHash("sha256").update(JSON.stringify(value), "utf8").digest("hex")}`;
}

export async function validateBackupDirectory(backupDir) {
  if (typeof backupDir !== "string" || !path.isAbsolute(backupDir)) {
    throw new PlatformCliError("backup directory must be an absolute path", { code: "invalid_backup" });
  }
  const root = path.resolve(backupDir);
  let rootMetadata;
  try {
    rootMetadata = await lstat(root);
  } catch (error) {
    throw new PlatformCliError("backup directory is unavailable", { code: "invalid_backup" });
  }
  if (rootMetadata.isSymbolicLink() || !rootMetadata.isDirectory()) {
    throw new PlatformCliError("backup directory must be a real directory", { code: "invalid_backup" });
  }
  let manifest;
  try {
    const manifestMetadata = await lstat(path.join(root, "manifest.json"));
    if (manifestMetadata.isSymbolicLink() || !manifestMetadata.isFile()) throw new Error("manifest is not regular");
    manifest = assertPlainObject(JSON.parse(await readFile(path.join(root, "manifest.json"), "utf8")), "backup manifest");
  } catch (error) {
    if (error instanceof PlatformCliError) throw error;
    throw new PlatformCliError("backup manifest is invalid", { code: "invalid_backup" });
  }
  if (
    manifest.schema_version !== 1
    || manifest.format !== "agent-knowledge-platform-backup/v1"
    || manifest.status !== "snapshot_created"
    || manifest.owner !== "puddingknowledge"
    || manifest.activation_allowed !== false
    || manifest.execution_allowed !== false
    || manifest.secret_bytes_copied !== false
    || manifest.source_data_mutation !== false
    || manifest.legacy_claw_mutation !== false
    || !Array.isArray(manifest.excluded_roots)
    || JSON.stringify(manifest.excluded_roots) !== JSON.stringify([...PLATFORM_BACKUP_EXCLUSIONS])
  ) {
    throw new PlatformCliError("backup manifest boundary is invalid", { code: "invalid_backup" });
  }
  const allowedManifestKeys = new Set([
    "schema_version",
    "format",
    "status",
    "owner",
    "created_at",
    "files",
    "file_count",
    "bytes_total",
    "excluded_roots",
    "secret_bytes_copied",
    "source_data_mutation",
    "legacy_claw_mutation",
    "execution_allowed",
    "activation_allowed",
    "manifest_digest",
  ]);
  if (Object.keys(manifest).some((key) => !allowedManifestKeys.has(key))) {
    throw new PlatformCliError("backup manifest contains unknown fields", { code: "invalid_backup" });
  }
  if (typeof manifest.created_at !== "string" || !manifest.created_at.trim()) {
    throw new PlatformCliError("backup manifest timestamp is invalid", { code: "invalid_backup" });
  }
  if (manifest.files === null || typeof manifest.files !== "object" || Array.isArray(manifest.files)) {
    throw new PlatformCliError("backup manifest files are invalid", { code: "invalid_backup" });
  }
  const declared = Object.entries(manifest.files).sort(([left], [right]) => left.localeCompare(right));
  const actual = await collectBackupFiles(root, "").catch((error) => {
    throw error instanceof PlatformCliError ? error : new PlatformCliError("backup files cannot be inspected", { code: "invalid_backup" });
  });
  const actualEntries = actual.map((entry) => entry.path).filter((relative) => relative !== "manifest.json").sort();
  const declaredPaths = declared.map(([relative]) => assertBackupRelative(relative));
  if (JSON.stringify(actualEntries) !== JSON.stringify(declaredPaths)) {
    throw new PlatformCliError("backup file set mismatch", { code: "invalid_backup" });
  }
  let bytesTotal = 0;
  for (const [relative, descriptor] of declared) {
    if (!assertPlainObject(descriptor, "backup file descriptor")
      || typeof descriptor.sha256 !== "string"
      || !/^sha256:[0-9a-f]{64}$/.test(descriptor.sha256)
      || !Number.isInteger(descriptor.bytes)
      || descriptor.bytes < 0) {
      throw new PlatformCliError("backup file descriptor is invalid", { code: "invalid_backup" });
    }
    const bytes = await readFile(path.join(root, relative));
    if (SECRET_MARKER_RE.test(bytes.toString("utf8"))) {
      throw new PlatformCliError("Platform backup contains secret-bearing bytes", { code: "secret_bearing_backup" });
    }
    const digest = `sha256:${createHash("sha256").update(bytes).digest("hex")}`;
    if (digest !== descriptor.sha256 || bytes.length !== descriptor.bytes) {
      throw new PlatformCliError("backup file digest mismatch", { code: "invalid_backup" });
    }
    bytesTotal += bytes.length;
  }
  if (manifest.file_count !== declared.length || manifest.bytes_total !== bytesTotal) {
    throw new PlatformCliError("backup manifest totals are invalid", { code: "invalid_backup" });
  }
  if (manifest.manifest_digest !== digestJson(backupManifestProjection(manifest))) {
    throw new PlatformCliError("backup manifest digest mismatch", { code: "invalid_backup" });
  }
  return {
    status: "valid",
    format: manifest.format,
    file_count: declared.length,
    bytes_total: bytesTotal,
    manifest_digest: manifest.manifest_digest,
    execution_allowed: false,
    activation_allowed: false,
  };
}

export async function validateMigrationManifest(manifestPath, backupManifestDigest) {
  if (typeof manifestPath !== "string" || !path.isAbsolute(manifestPath)) {
    throw new PlatformCliError("migration manifest must be an absolute path", { code: "invalid_migration_manifest" });
  }
  let metadata;
  try {
    metadata = await lstat(manifestPath);
  } catch (error) {
    throw new PlatformCliError("migration manifest is unavailable", { code: "invalid_migration_manifest" });
  }
  if (metadata.isSymbolicLink() || !metadata.isFile()) {
    throw new PlatformCliError("migration manifest must be a regular file", { code: "invalid_migration_manifest" });
  }
  let manifest;
  let raw;
  try {
    raw = await readFile(manifestPath, "utf8");
    manifest = assertPlainObject(JSON.parse(raw), "migration manifest");
  } catch (error) {
    if (error instanceof PlatformCliError) throw error;
    throw new PlatformCliError("migration manifest is invalid JSON", { code: "invalid_migration_manifest" });
  }
  const required = [
    "format",
    "source",
    "targets",
    "object_summaries",
    "id_resource_mappings",
    "credential_rebinds",
    "active_writers",
    "checkpoint",
    "rollback_strategy",
    "state",
    "snapshot_digest",
    "staging_namespace",
    "rollback_window_open",
  ];
  if (required.some((key) => !(key in manifest))) {
    throw new PlatformCliError("migration manifest is incomplete", { code: "invalid_migration_manifest" });
  }
  const allowedKeys = new Set([
    ...required,
    "active_installation_revision",
    "rollback_evidence_digest",
    "started_at",
    "completed_at",
  ]);
  if (Object.keys(manifest).some((key) => !allowedKeys.has(key))) {
    throw new PlatformCliError("migration manifest contains unknown fields", { code: "invalid_migration_manifest" });
  }
  const serialized = raw.toLowerCase();
  if (["/users/", "/private/", "file://", "password=", "secret=", "token="].some((marker) => serialized.includes(marker))) {
    throw new PlatformCliError("migration manifest contains a non-portable or secret marker", { code: "invalid_migration_manifest" });
  }
  if (
    manifest.format !== "agent-knowledge-platform-installation-migration/v1"
    || manifest.state !== "PREPARED"
    || manifest.rollback_window_open !== true
    || manifest.snapshot_digest !== backupManifestDigest
    || typeof manifest.staging_namespace !== "string"
    || !manifest.staging_namespace.trim()
    || manifest.active_installation_revision !== null
  ) {
    throw new PlatformCliError("migration manifest is not a prepared rollback-safe state", { code: "invalid_migration_manifest" });
  }
  const migrationObject = (value, label) => {
    if (value === null || typeof value !== "object" || Array.isArray(value)) {
      throw new PlatformCliError(`${label} is invalid`, { code: "invalid_migration_manifest" });
    }
    return value;
  };
  const boundedString = (value, label) => {
    if (typeof value !== "string" || value.length < 1 || value.length > 160) {
      throw new PlatformCliError(`${label} is invalid`, { code: "invalid_migration_manifest" });
    }
    return value;
  };
  const exactKeys = (value, expected, label) => {
    if (JSON.stringify(Object.keys(value).sort()) !== JSON.stringify([...expected].sort())) {
      throw new PlatformCliError(`${label} fields are invalid`, { code: "invalid_migration_manifest" });
    }
  };
  const source = migrationObject(manifest.source, "migration source");
  exactKeys(source, ["installation_id", "schema_revision", "catalog_revision"], "migration source");
  for (const field of ["installation_id", "schema_revision", "catalog_revision"]) boundedString(source[field], `migration source ${field}`);
  const targets = migrationObject(manifest.targets, "migration targets");
  if (Object.keys(targets).length < 1) throw new PlatformCliError("migration targets are empty", { code: "invalid_migration_manifest" });
  for (const [target, revision] of Object.entries(targets)) {
    boundedString(target, "migration target name");
    boundedString(revision, `migration target ${target}`);
  }
  if (!Array.isArray(manifest.object_summaries)) throw new PlatformCliError("migration object summaries are invalid", { code: "invalid_migration_manifest" });
  for (const summary of manifest.object_summaries) {
    const value = migrationObject(summary, "migration object summary");
    exactKeys(value, ["domain", "object_count", "source_digest"], "migration object summary");
    if (!["session_harness", "knowledge_catalog", "connector_jobs"].includes(value.domain)
      || !Number.isInteger(value.object_count)
      || value.object_count < 0
      || typeof value.source_digest !== "string"
      || !/^sha256:[0-9a-f]{64}$/.test(value.source_digest)) {
      throw new PlatformCliError("migration object summary is invalid", { code: "invalid_migration_manifest" });
    }
  }
  if (!Array.isArray(manifest.id_resource_mappings)) throw new PlatformCliError("migration resource mappings are invalid", { code: "invalid_migration_manifest" });
  for (const mapping of manifest.id_resource_mappings) {
    const value = migrationObject(mapping, "migration resource mapping");
    exactKeys(value, ["source_id", "resource_uri"], "migration resource mapping");
    boundedString(value.source_id, "migration source id");
    if (typeof value.resource_uri !== "string" || !/^(knowledge|harness):\/\/[^\s]+$/.test(value.resource_uri)) {
      throw new PlatformCliError("migration resource URI is invalid", { code: "invalid_migration_manifest" });
    }
  }
  if (!Array.isArray(manifest.credential_rebinds)) throw new PlatformCliError("migration credential rebinds are invalid", { code: "invalid_migration_manifest" });
  for (const rebind of manifest.credential_rebinds) {
    const value = migrationObject(rebind, "migration credential rebind");
    exactKeys(value, ["slot", "source_ref_digest", "target_ref", "status"], "migration credential rebind");
    boundedString(value.slot, "migration credential slot");
    if (typeof value.source_ref_digest !== "string"
      || !/^sha256:[0-9a-f]{64}$/.test(value.source_ref_digest)
      || typeof value.target_ref !== "string"
      || !/^credential:\/\/[^\s]+$/.test(value.target_ref)
      || value.status !== "pending") {
      throw new PlatformCliError("migration credential rebind is not pending and secret-free", { code: "invalid_migration_manifest" });
    }
  }
  const checkpoint = migrationObject(manifest.checkpoint, "migration checkpoint");
  for (const [key, value] of Object.entries(checkpoint)) {
    boundedString(key, "migration checkpoint key");
    boundedString(value, `migration checkpoint ${key}`);
  }
  if (typeof manifest.staging_namespace !== "string" || !/^[A-Za-z0-9._-]{1,160}$/.test(manifest.staging_namespace)) {
    throw new PlatformCliError("migration staging namespace is invalid", { code: "invalid_migration_manifest" });
  }
  if (!["reverse_delta", "snapshot_restore", "no_write_until_finalized"].includes(manifest.rollback_strategy)) {
    throw new PlatformCliError("migration rollback strategy is invalid", { code: "invalid_migration_manifest" });
  }
  for (const [field, value] of Object.entries({
    rollback_evidence_digest: manifest.rollback_evidence_digest,
    started_at: manifest.started_at,
    completed_at: manifest.completed_at,
  })) {
    if (value !== undefined && value !== null && typeof value !== "string") {
      throw new PlatformCliError(`migration ${field} is invalid`, { code: "invalid_migration_manifest" });
    }
  }
  if (manifest.rollback_evidence_digest !== undefined && manifest.rollback_evidence_digest !== null
    && !/^sha256:[0-9a-f]{64}$/.test(manifest.rollback_evidence_digest)) {
    throw new PlatformCliError("migration rollback evidence digest is invalid", { code: "invalid_migration_manifest" });
  }
  if (manifest.completed_at !== undefined && manifest.completed_at !== null && !manifest.completed_at.trim()) {
    throw new PlatformCliError("migration completed timestamp is invalid", { code: "invalid_migration_manifest" });
  }
  const writers = manifest.active_writers;
  if (
    writers === null
    || typeof writers !== "object"
    || Array.isArray(writers)
    || JSON.stringify(Object.keys(writers).sort()) !== JSON.stringify(["connector_jobs", "knowledge_catalog", "session_harness"])
    || Object.values(writers).some((writer) => writer !== "puddingclaw")
  ) {
    throw new PlatformCliError("migration manifest active writers are not source-owned", { code: "invalid_migration_manifest" });
  }
  const digest = `sha256:${createHash("sha256").update(raw, "utf8").digest("hex")}`;
  return {
    manifest_digest: digest,
    state: manifest.state,
    rollback_window_open: manifest.rollback_window_open,
  };
}

async function ensureRestoreTarget(backupDir, targetHome) {
  if (typeof targetHome !== "string" || !path.isAbsolute(targetHome)) {
    throw new PlatformCliError("restore target must be an absolute path", { code: "invalid_restore" });
  }
  const sourceRoot = await realpath(backupDir).catch(() => {
    throw new PlatformCliError("backup directory cannot be resolved", { code: "invalid_backup" });
  });
  const target = path.resolve(targetHome);
  const relative = path.relative(sourceRoot, target);
  if (!relative || (!relative.startsWith(`..${path.sep}`) && relative !== ".." && !path.isAbsolute(relative))) {
    throw new PlatformCliError("restore target must be outside the backup directory", { code: "invalid_restore" });
  }
  let parentMetadata;
  try {
    parentMetadata = await lstat(path.dirname(target));
  } catch (error) {
    if (error?.code === "ENOENT") throw new PlatformCliError("restore target parent must already exist", { code: "invalid_restore" });
    throw new PlatformCliError("restore target parent cannot be inspected", { code: "invalid_restore" });
  }
  if (parentMetadata.isSymbolicLink() || !parentMetadata.isDirectory()) {
    throw new PlatformCliError("restore target parent must be a real directory", { code: "invalid_restore" });
  }
  try {
    const targetMetadata = await lstat(target);
    if (targetMetadata.isSymbolicLink() || targetMetadata.isDirectory() || targetMetadata.isFile()) {
      throw new PlatformCliError("restore target already exists", { code: "restore_target_exists" });
    }
  } catch (error) {
    if (error instanceof PlatformCliError) throw error;
    if (error?.code !== "ENOENT") throw new PlatformCliError("restore target cannot be inspected", { code: "invalid_restore" });
  }
  return target;
}

export async function restorePlan(backupDir, targetHome) {
  const backup = await validateBackupDirectory(backupDir);
  const target = await ensureRestoreTarget(backupDir, targetHome);
  return {
    schema_version: 1,
    format: "agent-knowledge-platform-restore-plan/v1",
    status: "validated_not_executed",
    owner: "puddingknowledge",
    source_manifest_digest: backup.manifest_digest,
    source_file_count: backup.file_count,
    target_home: target,
    target_must_not_exist: true,
    execution_allowed: false,
    activation_allowed: false,
    source_data_mutation: false,
    legacy_claw_mutation: false,
    side_effects: [],
    next_boundary: "explicit_local_restore_apply",
  };
}

export async function restorePlatformHome(backupDir, targetHome) {
  const backup = await validateBackupDirectory(backupDir);
  const target = await ensureRestoreTarget(backupDir, targetHome);
  const sourceRoot = await realpath(backupDir);
  const manifest = assertPlainObject(JSON.parse(await readFile(path.join(sourceRoot, "manifest.json"), "utf8")), "backup manifest");
  const stage = `${target}.staging-${randomUUID()}`;
  try {
    await mkdir(stage, { recursive: false, mode: 0o700 });
    for (const [relative, descriptor] of Object.entries(manifest.files)) {
      const source = path.resolve(sourceRoot, relative);
      const destination = path.resolve(stage, relative);
      const destinationRelative = path.relative(stage, destination);
      if (destinationRelative.startsWith(`..${path.sep}`) || path.isAbsolute(destinationRelative)) {
        throw new PlatformCliError("restore file escapes target", { code: "invalid_restore" });
      }
      const bytes = await readFile(source);
      const digest = `sha256:${createHash("sha256").update(bytes).digest("hex")}`;
      if (digest !== descriptor.sha256 || bytes.length !== descriptor.bytes) {
        throw new PlatformCliError("backup file changed during restore", { code: "invalid_backup" });
      }
      await mkdir(path.dirname(destination), { recursive: true, mode: 0o700 });
      await writeFile(destination, bytes, { mode: 0o600 });
    }
    await ensureManagedDirectories(stage);
    const restoredConfig = await loadConfig(stage);
    if (!restoredConfig || restoredConfig.service !== "puddingknowledge" || restoredConfig.activation_allowed !== false) {
      throw new PlatformCliError("restored Platform config is invalid", { code: "invalid_restore" });
    }
    await rename(stage, target);
    return {
      schema_version: 1,
      format: "agent-knowledge-platform-restore/v1",
      status: "restored_local_not_running",
      owner: "puddingknowledge",
      source_manifest_digest: backup.manifest_digest,
      restored_file_count: backup.file_count,
      target_home: target,
      validation: "valid",
      execution_allowed: true,
      activation_allowed: false,
      process_started: false,
      production_endpoint: false,
      source_data_mutation: false,
      legacy_claw_mutation: false,
      side_effects: ["created_new_local_target_directory"],
    };
  } catch (error) {
    await rm(stage, { recursive: true, force: true }).catch(() => {});
    throw error instanceof PlatformCliError ? error : new PlatformCliError("Platform restore failed", { code: "invalid_restore" });
  }
}

export async function backupPlatformHome(home, outputDir) {
  let homeMetadata;
  try {
    homeMetadata = await lstat(home);
  } catch (error) {
    throw new PlatformCliError("Platform Home cannot be inspected", { code: "invalid_home" });
  }
  if (homeMetadata.isSymbolicLink() || !homeMetadata.isDirectory()) {
    throw new PlatformCliError("Platform Home must be a real directory", { code: "invalid_home" });
  }
  const config = await loadConfig(home);
  if (!config) throw new PlatformCliError("Platform is not initialized; run knowledge-platform init", { code: "not_initialized" });
  const target = await ensureBackupTarget(home, outputDir);
  const stage = `${target}.staging-${randomUUID()}`;
  const files = [];
  try {
    await mkdir(stage, { recursive: false, mode: 0o700 });
    for (const relative of PLATFORM_BACKUP_ROOTS) {
      const source = path.join(home, relative);
      let metadata;
      try {
        metadata = await lstat(source);
      } catch (error) {
        if (error?.code === "ENOENT") continue;
        throw new PlatformCliError("Platform backup source cannot be inspected", { code: "invalid_backup" });
      }
      if (metadata.isSymbolicLink()) {
        throw new PlatformCliError("Platform backup cannot contain symlinks", { code: "invalid_backup" });
      }
      if (metadata.isDirectory()) {
        await collectBackupFiles(source, relative, files);
      } else if (metadata.isFile()) {
        files.push({ path: assertBackupRelative(relative), source });
      } else {
        throw new PlatformCliError("Platform backup contains an unsupported file type", { code: "invalid_backup" });
      }
    }
    files.sort((left, right) => left.path.localeCompare(right.path));
    const descriptors = {};
    let bytesTotal = 0;
    for (const entry of files) {
      const before = await lstat(entry.source);
      const bytes = await readFile(entry.source);
      const after = await lstat(entry.source);
      if (before.size !== after.size || before.mtimeMs !== after.mtimeMs || before.ino !== after.ino) {
        throw new PlatformCliError("Platform backup source changed during snapshot", { code: "backup_source_changed" });
      }
      if (SECRET_MARKER_RE.test(bytes.toString("utf8"))) {
        throw new PlatformCliError("Platform backup refuses secret-bearing bytes", { code: "secret_bearing_backup" });
      }
      const destination = path.join(stage, entry.path);
      await mkdir(path.dirname(destination), { recursive: true, mode: 0o700 });
      await writeFile(destination, bytes, { mode: 0o600 });
      const digest = `sha256:${createHash("sha256").update(bytes).digest("hex")}`;
      descriptors[entry.path] = { sha256: digest, bytes: bytes.length };
      bytesTotal += bytes.length;
    }
    const manifest = {
      schema_version: 1,
      format: "agent-knowledge-platform-backup/v1",
      status: "snapshot_created",
      owner: "puddingknowledge",
      created_at: new Date().toISOString(),
      files: descriptors,
      file_count: files.length,
      bytes_total: bytesTotal,
      excluded_roots: [...PLATFORM_BACKUP_EXCLUSIONS],
      secret_bytes_copied: false,
      source_data_mutation: false,
      legacy_claw_mutation: false,
      execution_allowed: false,
      activation_allowed: false,
    };
    const completeManifest = { ...manifest, manifest_digest: digestJson(manifest) };
    await writeFile(path.join(stage, "manifest.json"), `${JSON.stringify(completeManifest, null, 2)}\n`, { mode: 0o600 });
    await validateBackupDirectory(stage);
    await rename(stage, target);
    return {
      ...completeManifest,
      backup_directory: target,
      validation: "valid",
    };
  } catch (error) {
    await rm(stage, { recursive: true, force: true }).catch(() => {});
    throw error instanceof PlatformCliError ? error : new PlatformCliError("Platform backup failed", { code: "invalid_backup" });
  }
}

export function defaultConfig() {
  return {
    schema_version: 1,
    service: "puddingknowledge",
    initialized: true,
    catalog: { mode: "sqlite_local", relative_path: "catalog/knowledge-platform.sqlite3" },
    runtime: { status: "not_attached", process_started: false },
    infrastructure: {
      owner: "puddingknowledge",
      status: "not_started",
      process_started: false,
      compose_asset: "assets/compose.platform.yml",
    },
    activation_allowed: false,
  };
}

export async function loadConfig(home) {
  await assertNoSymlinkAncestors(home, "Platform Home", "invalid_home");
  const config = await readJson(path.join(home, CONFIG_FILE));
  if (config === null) return null;
  if (
    config.schema_version !== 1
    || config.service !== "puddingknowledge"
    || config.initialized !== true
    || config.activation_allowed !== false
    || !config.infrastructure
    || typeof config.infrastructure !== "object"
    || Array.isArray(config.infrastructure)
    || config.infrastructure.owner !== "puddingknowledge"
  ) {
    throw new PlatformCliError("Platform config schema or ownership is invalid", { code: "invalid_state" });
  }
  return config;
}

export async function initialize(home, { force = false } = {}) {
  await ensureHome(home);
  await ensureManagedDirectories(home);
  const existing = await loadConfig(home);
  if (existing && !force) return { status: "already_initialized", config: existing, changed: false };
  const config = defaultConfig();
  await writeJsonAtomic(path.join(home, CONFIG_FILE), config);
  return { status: "initialized", config, changed: true };
}

export function validateRuntimeManifest(manifest) {
  assertPlainObject(manifest, "runtime manifest");
  if (manifest.schema_version !== 1 || typeof manifest.release_version !== "string" || !manifest.release_version.trim()) {
    throw new PlatformCliError("runtime manifest identity is invalid", { code: "invalid_runtime_bundle" });
  }
  if (manifest.files === null || typeof manifest.files !== "object" || Array.isArray(manifest.files)
    || Object.keys(manifest.files).length === 0) {
    throw new PlatformCliError("runtime manifest files are invalid", { code: "invalid_runtime_bundle" });
  }
  for (const [file, digest] of Object.entries(manifest.files)) {
    assertRelative(file, "runtime manifest file");
    if (file === "manifest.json" || file.includes("*")) {
      throw new PlatformCliError("runtime manifest file must be a concrete bundle path", { code: "invalid_runtime_bundle" });
    }
    if (typeof digest !== "string" || !/^[0-9a-f]{64}$/.test(digest)) {
      throw new PlatformCliError("runtime manifest digest is invalid", { code: "invalid_runtime_bundle" });
    }
  }
  return manifest;
}

async function readStableRegularFile(file, label) {
  let before;
  try {
    before = await lstat(file);
  } catch {
    throw new PlatformCliError(`${label} is unavailable`, { code: "invalid_runtime_bundle" });
  }
  if (before.isSymbolicLink() || !before.isFile()) {
    throw new PlatformCliError(`${label} must be a regular file`, { code: "invalid_runtime_bundle" });
  }
  let bytes;
  try {
    bytes = await readFile(file);
  } catch {
    throw new PlatformCliError(`${label} cannot be read`, { code: "invalid_runtime_bundle" });
  }
  const after = await lstat(file).catch(() => null);
  if (!after || after.isSymbolicLink() || !after.isFile()
    || before.size !== after.size || before.mtimeMs !== after.mtimeMs || before.ino !== after.ino) {
    throw new PlatformCliError(`${label} changed during validation`, { code: "invalid_runtime_bundle" });
  }
  return { bytes, mode: before.mode & 0o7777 };
}

async function inspectRuntimeBundle(bundlePath) {
  if (typeof bundlePath !== "string" || !path.isAbsolute(bundlePath)) {
    throw new PlatformCliError("runtime bundle must be an absolute path", { code: "invalid_runtime_bundle" });
  }
  await assertNoSymlinkAncestors(bundlePath, "runtime bundle", "invalid_runtime_bundle");
  const bundleMetadata = await lstat(bundlePath).catch(() => null);
  if (!bundleMetadata || bundleMetadata.isSymbolicLink() || !bundleMetadata.isDirectory()) {
    throw new PlatformCliError("runtime bundle must be a real directory", { code: "invalid_runtime_bundle" });
  }
  const bundleRoot = await realpath(bundlePath).catch(() => {
    throw new PlatformCliError("runtime bundle cannot be resolved", { code: "invalid_runtime_bundle" });
  });
  const manifestPath = path.join(bundleRoot, "manifest.json");
  let manifest;
  try {
    const manifestFile = await readStableRegularFile(manifestPath, "runtime bundle manifest");
    manifest = validateRuntimeManifest(JSON.parse(manifestFile.bytes.toString("utf8")));
    const declaredFiles = Object.keys(manifest.files).sort();
    const actualFiles = (await bundleFiles(bundleRoot)).sort();
    if (JSON.stringify(declaredFiles) !== JSON.stringify(actualFiles)) {
      throw new Error("runtime bundle file set mismatch");
    }
    for (const [file, expectedDigest] of Object.entries(manifest.files)) {
      const candidate = path.resolve(bundleRoot, file);
      const relative = path.relative(bundleRoot, candidate);
      if (relative.startsWith("..") || path.isAbsolute(relative)) throw new Error("file outside bundle");
      const candidateFile = await readStableRegularFile(candidate, `runtime bundle file ${file}`);
      const digest = createHash("sha256").update(candidateFile.bytes).digest("hex");
      if (digest !== expectedDigest) throw new Error("runtime bundle file digest mismatch");
    }
    return {
      bundleRoot,
      manifest,
      digest: createHash("sha256").update(JSON.stringify(manifest), "utf8").digest("hex"),
    };
  } catch (error) {
    if (error instanceof PlatformCliError) throw error;
    throw new PlatformCliError("runtime bundle manifest or file digest is invalid", { code: "invalid_runtime_bundle" });
  }
}

export async function verifyRuntimeBundle(bundlePath) {
  const { manifest, digest } = await inspectRuntimeBundle(bundlePath);
  return { manifest, digest };
}

async function copyStableFile(source, destination, label) {
  const sourceFile = await readStableRegularFile(source, label);
  await mkdir(path.dirname(destination), { recursive: true, mode: 0o700 });
  await writeFile(destination, sourceFile.bytes, { mode: sourceFile.mode });
  await chmod(destination, sourceFile.mode);
}

async function verifyPublishedRuntimeRelease(releaseRoot, expectedManifest, expectedDigest) {
  const metadata = await lstat(releaseRoot).catch(() => null);
  if (!metadata || metadata.isSymbolicLink() || !metadata.isDirectory()) {
    throw new PlatformCliError("runtime release is not a real directory", { code: "runtime_release_conflict" });
  }
  let inspected;
  try {
    inspected = await inspectRuntimeBundle(releaseRoot);
  } catch {
    throw new PlatformCliError("existing runtime release does not match manifest", { code: "runtime_release_conflict" });
  }
  if (inspected.digest !== expectedDigest || JSON.stringify(inspected.manifest) !== JSON.stringify(expectedManifest)) {
    throw new PlatformCliError("existing runtime release does not match manifest", { code: "runtime_release_conflict" });
  }
  return inspected;
}

async function ensureRuntimeReleaseRoot(home) {
  const runtimeRoot = path.join(home, "runtime");
  const runtimeMetadata = await lstat(runtimeRoot).catch(() => null);
  if (!runtimeMetadata || runtimeMetadata.isSymbolicLink() || !runtimeMetadata.isDirectory()) {
    throw new PlatformCliError("Platform runtime directory is unsafe", { code: "invalid_home" });
  }
  const releasesRoot = path.join(runtimeRoot, "releases");
  const releasesMetadata = await lstat(releasesRoot).catch(() => null);
  if (releasesMetadata) {
    if (releasesMetadata.isSymbolicLink() || !releasesMetadata.isDirectory()) {
      throw new PlatformCliError("Platform runtime releases directory is unsafe", { code: "invalid_home" });
    }
  } else {
    await mkdir(releasesRoot, { mode: 0o700 });
  }
  return releasesRoot;
}

async function stageDeploymentUnlocked(home, bundlePath) {
  const inspectedBundle = await inspectRuntimeBundle(bundlePath);
  const { bundleRoot, manifest, digest } = inspectedBundle;
  const releasesRoot = await ensureRuntimeReleaseRoot(home);
  const releaseRoot = path.join(releasesRoot, digest);
  let releaseCreated = false;
  const existingRelease = await lstat(releaseRoot).catch(() => null);
  if (existingRelease) {
    await verifyPublishedRuntimeRelease(releaseRoot, manifest, digest);
  } else {
    const temporaryRelease = path.join(releasesRoot, `.release-${digest}-${randomUUID()}.tmp`);
    try {
      await mkdir(temporaryRelease, { mode: 0o700 });
      await copyStableFile(path.join(bundleRoot, "manifest.json"), path.join(temporaryRelease, "manifest.json"), "runtime bundle manifest");
      for (const file of Object.keys(manifest.files)) {
        await copyStableFile(path.join(bundleRoot, file), path.join(temporaryRelease, file), `runtime bundle file ${file}`);
      }
      await verifyPublishedRuntimeRelease(temporaryRelease, manifest, digest);
      await rename(temporaryRelease, releaseRoot);
      releaseCreated = true;
    } catch (error) {
      await rm(temporaryRelease, { recursive: true, force: true }).catch(() => {});
      if (error instanceof PlatformCliError) throw error;
      throw new PlatformCliError("runtime release could not be published", { code: "runtime_release_publish_failed" });
    }
  }
  const deployment = {
    schema_version: 1,
    status: "staged_not_running",
    activation_allowed: false,
    processes_started: false,
    runtime_release: manifest.release_version,
    runtime_file_count: Object.keys(manifest.files).length,
    runtime_manifest_digest: `sha256:${digest}`,
    runtime_bundle_path: releaseRoot,
    execution: "deferred_to_platform_supervisor",
  };
  try {
    await writeJsonAtomic(path.join(home, DEPLOYMENT_FILE), deployment);
  } catch (error) {
    if (releaseCreated) await rm(releaseRoot, { recursive: true, force: true }).catch(() => {});
    throw error;
  }
  return deployment;
}

export async function stageDeployment(home, bundlePath) {
  await assertExistingHomeDirectory(home);
  const lockPath = await acquireRuntimeStageLock(home);
  try {
    return await stageDeploymentUnlocked(home, bundlePath);
  } finally {
    await rm(lockPath, { force: true }).catch(() => {});
  }
}

export async function stageOperation(home, kind, details = {}) {
  const operationId = `op_${randomUUID().replaceAll("-", "")}`;
  const operation = {
    schema_version: 1,
    operation_id: operationId,
    kind,
    status: "staged_not_executed",
    execution_allowed: false,
    activation_allowed: false,
    details: { ...details },
  };
  await writeJsonAtomic(path.join(home, OPERATIONS_DIR, `${operationId}.json`), operation);
  return operation;
}

export function infrastructurePlan(home) {
  return {
    schema_version: 1,
    kind: "infrastructure",
    status: "plan",
    owner: "puddingknowledge",
    home_digest: `sha256:${createHash("sha256").update(home, "utf8").digest("hex")}`,
    services: [...PLATFORM_INFRASTRUCTURE_SERVICES],
    persistent_directories: [
      "infrastructure/postgres",
      "infrastructure/milvus/etcd",
      "infrastructure/milvus/minio",
      "infrastructure/milvus/data",
    ],
    legacy_dependencies: [],
    secret_inputs: [
      "PUDDINGKNOWLEDGE_POSTGRES_USER",
      "PUDDINGKNOWLEDGE_POSTGRES_PASSWORD",
      "PUDDINGKNOWLEDGE_MINIO_ROOT_USER",
      "PUDDINGKNOWLEDGE_MINIO_ROOT_PASSWORD",
      "PUDDINGKNOWLEDGE_API_IMAGE",
      "PUDDINGKNOWLEDGE_WORKER_IMAGE",
      "PUDDINGKNOWLEDGE_CONSOLE_IMAGE",
    ],
    execution_allowed: false,
    process_started: false,
    activation_allowed: false,
    side_effects: [],
    next_boundary: "operator_compose_supervisor",
  };
}

export function lifecyclePlan(kind, home) {
  if (!PLATFORM_LIFECYCLE_OPERATIONS.includes(kind)) {
    throw new PlatformCliError("Platform lifecycle operation is invalid", { code: "invalid_operation" });
  }
  const nextBoundary = {
    health: "operator_supervisor_health_probe",
    backup: "platform_snapshot_runner",
    upgrade: "migration_manifest_and_rollback_window",
  }[kind];
  const requiredPreconditions = {
    health: [],
    backup: ["initialized_platform_home"],
    upgrade: ["validated_backup_snapshot", "migration_manifest", "rollback_window_open"],
  }[kind];
  return {
    schema_version: 1,
    format: "agent-knowledge-platform-lifecycle-plan/v1",
    kind,
    status: "plan",
    owner: "puddingknowledge",
    home_digest: `sha256:${createHash("sha256").update(home, "utf8").digest("hex")}`,
    platform_owned: true,
    execution_allowed: false,
    activation_allowed: false,
    process_started: false,
    source_data_mutation: false,
    legacy_claw_mutation: false,
    secret_bytes_read: false,
    side_effects: [],
    required_preconditions: requiredPreconditions,
    next_boundary: nextBoundary,
  };
}

export function validateComposeAsset(composeText) {
  if (typeof composeText !== "string" || !composeText.trim()) {
    throw new PlatformCliError("Platform Compose asset is empty", { code: "invalid_infrastructure_asset" });
  }
  if (/puddingclaw|docker-compose\.infra/i.test(composeText)) {
    throw new PlatformCliError("Platform Compose asset references legacy Claw infrastructure", { code: "invalid_infrastructure_asset" });
  }
  if (!/^name:\s*puddingknowledge\s*$/m.test(composeText)) {
    throw new PlatformCliError("Platform Compose asset has an invalid project name", { code: "invalid_infrastructure_asset" });
  }
  const services = [...composeText.matchAll(/^  ([a-z][a-z0-9-]*):\s*$/gm)].map((match) => match[1]);
  if (JSON.stringify(services) !== JSON.stringify([...REQUIRED_COMPOSE_SERVICES])) {
    throw new PlatformCliError("Platform Compose service set is invalid", { code: "invalid_infrastructure_asset" });
  }
  for (const input of REQUIRED_COMPOSE_SECRET_INPUTS) {
    const marker = new RegExp(`\\$\\{${input}:\\?set\\s+${input}\\}`);
    if (!marker.test(composeText)) {
      throw new PlatformCliError(`Platform Compose secret input is not required: ${input}`, { code: "invalid_infrastructure_asset" });
    }
  }
  for (const line of composeText.split(/\r?\n/)) {
    const password = line.match(/^\s+[A-Z_]*PASSWORD:\s*(.*)$/i);
    if (password && !password[1].startsWith("${")) {
      throw new PlatformCliError("Platform Compose contains a plaintext password", { code: "invalid_infrastructure_asset" });
    }
  }
  const digest = createHash("sha256").update(composeText, "utf8").digest("hex");
  return {
    format: "compose-spec",
    digest: `sha256:${digest}`,
    services: [...services],
    secret_inputs: [...REQUIRED_COMPOSE_SECRET_INPUTS],
  };
}

export async function stageInfrastructure(home, composeText) {
  const compose = validateComposeAsset(composeText);
  const plan = infrastructurePlan(home);
  return stageOperation(home, "infrastructure", {
    owner: plan.owner,
    services: plan.services,
    compose_asset: "assets/compose.platform.yml",
    compose_digest: compose.digest,
    compose_services: compose.services,
    secret_inputs: compose.secret_inputs,
    execution_allowed: false,
    process_started: false,
    activation_allowed: false,
  });
}

export async function status(home) {
  const config = await loadConfig(home);
  if (!config) return { status: "not_initialized", initialized: false, activation_allowed: false };
  const deployment = await readJson(path.join(home, DEPLOYMENT_FILE));
  return {
    status: deployment?.status || "initialized",
    initialized: config.initialized === true,
    service: config.service,
    catalog_mode: config.catalog?.mode,
    runtime: deployment || config.runtime,
    activation_allowed: false,
  };
}

export async function health(home) {
  const config = await loadConfig(home);
  if (!config) {
    return {
      schema_version: 1,
      format: "agent-knowledge-platform-health-shadow/v1",
      status: "not_initialized",
      owner: "puddingknowledge",
      execution_allowed: false,
      activation_allowed: false,
      process_started: false,
      side_effects: [],
    };
  }
  const deployment = await readJson(path.join(home, DEPLOYMENT_FILE));
  const infrastructureProcessStarted = config.infrastructure?.process_started === true;
  const runtimeProcessStarted = deployment?.processes_started === true || config.runtime?.process_started === true;
  return {
    schema_version: 1,
    format: "agent-knowledge-platform-health-shadow/v1",
    status: infrastructureProcessStarted || runtimeProcessStarted
      ? "shadow_metadata_claims_started_unprobed"
      : "shadow_observed_not_running",
    owner: "puddingknowledge",
    initialized: true,
    infrastructure: {
      status: config.infrastructure?.status || "not_started",
      process_started: infrastructureProcessStarted,
    },
    runtime: {
      status: deployment?.status || config.runtime?.status || "not_attached",
      process_started: runtimeProcessStarted,
    },
    observation_scope: "Platform Home metadata only; Docker, external providers, and production endpoints were not probed",
    execution_allowed: false,
    activation_allowed: false,
    process_started: false,
    side_effects: [],
  };
}
