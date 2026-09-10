/** Install a verified owned release without mutating its content-addressed tree. */
import { randomUUID } from 'node:crypto';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { lstat, mkdir, readFile, writeFile, rename, rm, cp, open } from 'node:fs/promises';
import path from 'node:path';
import { PlatformCliError, ensureHome, loadConfig, verifyRuntimeBundle } from './state.mjs';

const execute = promisify(execFile);
const error = (message, code = 'runtime_unavailable') => new PlatformCliError(message, { code });
const DIGEST = /^sha256:([0-9a-f]{64})$/;

export async function checkedPath(file, { missing = false } = {}) {
  if (!path.isAbsolute(file)) throw error('Runtime paths must be absolute', 'invalid_path');
  let current = path.parse(file).root;
  for (const part of file.slice(current.length).split(path.sep).filter(Boolean)) {
    current = path.join(current, part);
    let info;
    try { info = await lstat(current); }
    catch (e) { if (e.code === 'ENOENT' && missing) continue; throw error('Runtime path is unavailable'); }
    if (info.isSymbolicLink()) throw error('Runtime path contains a symlink', 'invalid_path');
  }
  return file;
}

async function readState(file) {
  await checkedPath(file);
  const info = await lstat(file);
  if (!info.isFile() || info.size > 1024 * 1024) throw error('Runtime state is invalid');
  let value;
  try { value = JSON.parse(await readFile(file, 'utf8')); }
  catch { throw error('Runtime state is invalid'); }
  if (!value || Array.isArray(value) || typeof value !== 'object') throw error('Runtime state is invalid');
  return value;
}

async function deployment(home) {
  await checkedPath(home);
  if (!(await loadConfig(home))) throw error('Platform is not initialized', 'not_initialized');
  const value = await readState(path.join(home, 'deployment.json'));
  if (value.schema_version !== 1 || value.status !== 'staged_not_running' || value.activation_allowed !== false || value.processes_started !== false) {
    throw error('Deployment is not a valid inactive staged release');
  }
  const digest = DIGEST.exec(value.runtime_manifest_digest || '')?.[1];
  if (!digest) throw error('Deploy a verified runtime bundle first');
  const release = path.join(home, 'runtime', 'releases', digest);
  if (value.runtime_bundle_path !== release) throw error('Deployment release path is not owned by this Home');
  await checkedPath(release);
  const verified = await verifyRuntimeBundle(release);
  if (value.runtime_file_count !== Object.keys(verified.manifest.files).length || value.runtime_release !== verified.manifest.release_version) throw error('Deployment identity does not match its manifest');
  if (verified.digest !== digest) throw error('Owned release digest no longer matches deployment');
  for (const required of ['pyproject.toml', 'uv.lock', 'knowledge_platform/local/supervisor.py']) {
    if (!Object.hasOwn(verified.manifest.files, required)) throw error('Bundle does not contain an executable local runtime');
  }
  return { digest, release };
}

function environment(home) {
  const env = { ...process.env, PUDDINGKNOWLEDGE_HOME: home, PYTHONDONTWRITEBYTECODE: '1' };
  delete env.PYTHONPATH;
  delete env.PYTHONHOME;
  delete env.PUDDINGCLAW_HOME;
  delete env.VIRTUAL_ENV;
  return env;
}

export async function installRuntime(home, { runner = execute } = {}) {
  await ensureHome(home);
  const { digest, release } = await deployment(home);
  const build = path.join(home, 'builds', randomUUID());
  const target = path.join(home, 'environments', digest);
  for (const dir of [path.dirname(build), path.dirname(target)]) {
    await checkedPath(dir, { missing: true });
    await mkdir(dir, { recursive: true, mode: 0o700 });
  }
  await checkedPath(target, { missing: true });
  let lock;
  try { lock = await open(path.join(home, '.runtime-install.lock'), 'wx', 0o600); }
  catch { throw error('Another installation is active or requires inspection', 'installation_locked'); }
  let created = false;
  let temporary;
  const python = path.join(target, process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
  const probeInstalled = async () => {
    const probe = await runner(python, ['-c', "import knowledge_platform.local.supervisor as s; assert 'site-packages' in s.__file__; print('INSTALLED_RUNTIME_OK')"], {
      cwd: home, env: environment(home), timeout: 15000, maxBuffer: 65536,
    });
    if (probe.stdout.trim() !== 'INSTALLED_RUNTIME_OK') throw error('Installed runtime probe failed');
  };
  try {
    let exists = false;
    try { await lstat(target); exists = true; }
    catch (e) { if (e.code !== 'ENOENT') throw e; }
    if (exists) {
      const installed = await readState(path.join(home, 'runtime', 'installed.json'));
      if (installed.schema_version !== 1 || installed.activation_allowed !== false || installed.runtime_manifest_digest !== `sha256:${digest}` || installed.status !== 'installed_not_running') {
        throw error('Existing environment has no matching installation record', 'environment_exists');
      }
      await probeInstalled();
      return { ...installed, reused: true };
    }
    await mkdir(target, { mode: 0o700 });
    created = true;
    await cp(release, build, { recursive: true, errorOnExist: true, force: false });
    const copied = await verifyRuntimeBundle(build);
    if (copied.digest !== digest) throw error('Runtime changed during installation staging');
    await runner('uv', ['sync', '--project', build, '--locked', '--no-editable', '--no-dev', '--all-extras'], {
      cwd: home, env: { ...environment(home), UV_PROJECT_ENVIRONMENT: target, UV_LINK_MODE: 'copy' },
      timeout: 300000, maxBuffer: 4 * 1024 * 1024,
    });
    await probeInstalled();
    // Recheck the source release after uv/build hooks completed.
    if ((await verifyRuntimeBundle(release)).digest !== digest) throw error('Owned release changed during installation');
    const record = { schema_version: 1, status: 'installed_not_running', runtime_manifest_digest: `sha256:${digest}`, activation_allowed: false };
    temporary = path.join(home, 'runtime', `.installed-${randomUUID()}.json`);
    await writeFile(temporary, JSON.stringify(record) + '\n', { mode: 0o600, flag: 'wx' });
    await rename(temporary, path.join(home, 'runtime', 'installed.json'));
    created = false;
    return record;
  } catch (e) {
    if (created) await rm(target, { recursive: true, force: true });
    if (e instanceof PlatformCliError) throw e;
    throw error('Runtime installation failed; no installation was published', 'installation_failed');
  } finally {
    try {
      await rm(build, { recursive: true, force: true });
      if (temporary) await rm(temporary, { force: true });
    } finally {
      await lock.close();
      await rm(path.join(home, '.runtime-install.lock'));
    }
  }
}

export async function runtimeCommand(command, home, flags = {}) {
  await checkedPath(home);
  const installed = await readState(path.join(home, 'runtime', 'installed.json'));
  const digest = DIGEST.exec(installed.runtime_manifest_digest || '')?.[1];
  if (!digest || installed.schema_version !== 1 || installed.activation_allowed !== false || installed.status !== 'installed_not_running') throw error('Runtime installation state is invalid');
  const target = path.join(home, 'environments', digest);
  await checkedPath(target);
  const python = path.join(target, process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
  // venv Python may itself be a standard interpreter symlink; its parent must be owned.
  await checkedPath(path.dirname(python));
  const args = ['-m', 'knowledge_platform.local.supervisor', command, '--home', home];
  if (command === 'start') {
    const current = await deployment(home);
    if (current.digest !== digest) throw error('Install the current staged release before starting');
    for (const name of ['catalog', 'wiki_root', 'port']) {
      if (!flags[name]) throw error(`start requires --${name.replaceAll('_', '-')}`, 'argument_error');
    }
    for (const name of ['catalog', 'wiki_root', 'port', 'database_config', 'structured_config', 'state_dir', 'wiki_config', 'capture_config', 'feishu_config', 'file_config', 'package_config']) {
      if (flags[name]) args.push(`--${name.replaceAll('_', '-')}`, String(flags[name]));
    }
  }
  let result;
  try { result = await execute(python, args, { cwd: home, env: environment(home), timeout: 45000, maxBuffer: 1024 * 1024 }); }
  catch (e) {
    try { const report = JSON.parse(e.stdout); throw error(report.error || 'Runtime control failed', report.code || 'runtime_control_failed'); }
    catch (parsed) { if (parsed instanceof PlatformCliError) throw parsed; throw error('Runtime control failed', 'runtime_control_failed'); }
  }
  let observed;
  try { observed = JSON.parse(result.stdout); }
  catch { throw error('Runtime returned invalid control evidence'); }
  // Supervisor v1 uses state internally; CLI exposes the normalized status.
  if (observed?.format === 'puddingknowledge-local-supervisor/v1' && observed.status === undefined) {
    observed.status = observed.state === 'stale' ? 'unknown' : observed.state;
  }
  if (!observed || observed.format !== 'puddingknowledge-local-supervisor/v1'
      || !['running', 'starting', 'stopped', 'failed', 'unknown', 'unhealthy'].includes(observed.status)) {
    throw error('Runtime returned invalid control evidence');
  }
  if (observed.status === 'running' && (observed.active !== true || observed.health !== true
      || observed.ownership_verified !== true || !Number.isInteger(observed.manager_pid) || observed.manager_pid <= 0
      || !Number.isInteger(observed.child_pid) || observed.child_pid <= 0)) {
    throw error('Runtime running state has no live ownership and health evidence');
  }
  return observed;
}

/** Execute a host-bound Package operation on this Home's live owned runtime. */
export async function packageCommand(command, home, body) {
  if (!['import', 'export'].includes(command)) throw error('Invalid Package operation', 'argument_error');
  const observed = await runtimeCommand('status', home);
  const instance = path.basename(observed.run_dir || '');
  if (observed.status !== 'running' || !Number.isInteger(observed.port)
      || observed.port < 1 || observed.port > 65535 || !/^[0-9a-f]{32}$/.test(instance)) {
    throw error('Package operations require a healthy owned runtime');
  }
  const response = await fetch(`http://127.0.0.1:${observed.port}/v1/packages:${command}`, {
    method: 'POST', redirect: 'error', headers: { 'Content-Type': 'application/json', 'X-PuddingKnowledge-Expected-Instance': instance },
    body: JSON.stringify(body), signal: AbortSignal.timeout(120000),
  });
  if (response.headers.get('X-PuddingKnowledge-Instance') !== instance || !response.ok) {
    throw error('Package runtime ownership or response is invalid');
  }
  let result;
  try { result = await response.json(); } catch { throw error('Package response is invalid'); }
  if (result?.status !== 'ok' || !result.data || typeof result.data !== 'object') {
    throw error(result?.error?.message || 'Package operation failed', 'package_operation_failed');
  }
  return result;
}
