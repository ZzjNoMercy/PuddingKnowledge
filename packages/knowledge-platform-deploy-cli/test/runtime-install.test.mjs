import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdtemp, mkdir, writeFile, readFile, readdir, rm, realpath, symlink } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { initialize, stageDeployment } from '../src/state.mjs';
import { installRuntime, checkedPath, runtimeCommand } from '../src/runtime-install.mjs';

async function fixture(t) {
  const root = await realpath(await mkdtemp(path.join(os.tmpdir(), 'knowledge-install-')));
  t.after(() => rm(root, { recursive: true, force: true }));
  const home = path.join(root, 'home');
  const source = path.join(root, 'bundle');
  await mkdir(path.join(source, 'knowledge_platform/local'), { recursive: true });
  const content = { 'pyproject.toml': '[project]\nname="fixture"\n', 'uv.lock': '# fixture', 'knowledge_platform/local/supervisor.py': '# fixture only' };
  for (const [name, bytes] of Object.entries(content)) await writeFile(path.join(source, name), bytes);
  const manifest = { schema_version: 1, release_version: 'test', files: Object.fromEntries(Object.entries(content).map(([name, value]) => [name, createHash('sha256').update(value).digest('hex')])) };
  await writeFile(path.join(source, 'manifest.json'), JSON.stringify(manifest));
  await initialize(home);
  const staged = await stageDeployment(home, source);
  return { root, home, source, staged };
}

test('installer builds a disposable copy and publishes only after installed import probe', async t => {
  const { home, source } = await fixture(t);
  const calls = [];
  const runner = async (file, args, options) => {
    calls.push({ file, args, options });
    if (file === 'uv') {
      assert.deepEqual(args.slice(0, 2), ['sync', '--project']);
      assert.ok(args.includes('--locked') && args.includes('--no-editable') && args.includes('--no-dev'));
      assert.ok(args[2].startsWith(path.join(home, 'builds') + path.sep));
      assert.ok(options.env.UV_PROJECT_ENVIRONMENT.startsWith(path.join(home, 'environments') + path.sep));
      assert.equal(options.env.PYTHONPATH, undefined);
      assert.equal(options.env.PUDDINGCLAW_HOME, undefined);
      await writeFile(path.join(args[2], 'build-hook-output'), 'ephemeral');
      return { stdout: '' };
    }
    assert.ok(args[1].includes('site-packages'));
    return { stdout: 'INSTALLED_RUNTIME_OK\n' };
  };
  const result = await installRuntime(home, { runner });
  assert.equal(result.status, 'installed_not_running');
  assert.equal(calls.length, 2);
  assert.deepEqual(await readdir(path.join(home, 'builds')), []);
  assert.equal((await readdir(source)).includes('build-hook-output'), false);
  assert.deepEqual(JSON.parse(await readFile(path.join(home, 'runtime/installed.json'))), result);
});

test('failed install removes only its new environment and publishes no state', async t => {
  const { home } = await fixture(t);
  await assert.rejects(installRuntime(home, { runner: async () => { throw new Error('fixture failure'); } }), /installation failed/);
  assert.deepEqual(await readdir(path.join(home, 'environments')), []);
  await assert.rejects(readFile(path.join(home, 'runtime/installed.json')), { code: 'ENOENT' });
  await assert.rejects(readFile(path.join(home, '.runtime-install.lock')), { code: 'ENOENT' });
});

test('failed installed import probe cannot publish a runnable state', async t => {
  const { home } = await fixture(t);
  await assert.rejects(installRuntime(home, { runner: async () => ({ stdout: 'not installed' }) }), /probe failed/);
  assert.deepEqual(await readdir(path.join(home, 'environments')), []);
});

test('tampered owned release is rejected before spawning installer', async t => {
  const { home, staged } = await fixture(t);
  const digest = staged.runtime_manifest_digest.slice(7);
  await writeFile(path.join(home, 'runtime/releases', digest, 'uv.lock'), 'tampered');
  let called = false;
  await assert.rejects(installRuntime(home, { runner: async () => { called = true; } }));
  assert.equal(called, false);
});

test('runtime path checks reject relative and parent symlink paths', async t => {
  const { root, home } = await fixture(t);
  await assert.rejects(checkedPath('relative'), /absolute/);
  await symlink(home, path.join(root, 'link'));
  await assert.rejects(checkedPath(path.join(root, 'link', 'future'), { missing: true }), /symlink/);
});

test('repeating a complete install probes and reuses its environment', async t => {
  const { home } = await fixture(t);
  let syncs = 0;
  const runner = async file => {
    if (file === 'uv') { syncs++; return { stdout: '' }; }
    return { stdout: 'INSTALLED_RUNTIME_OK' };
  };
  await installRuntime(home, { runner });
  const second = await installRuntime(home, { runner });
  assert.equal(syncs, 1);
  assert.equal(second.reused, true);
});

test('preexisting incomplete environment and installation lock are never removed', async t => {
  const { home, staged } = await fixture(t);
  const target = path.join(home, 'environments', staged.runtime_manifest_digest.slice(7));
  await mkdir(target, { recursive: true });
  await writeFile(path.join(target, 'sentinel'), 'keep');
  await assert.rejects(installRuntime(home, { runner: async () => { throw new Error('must not spawn'); } }));
  assert.equal(await readFile(path.join(target, 'sentinel'), 'utf8'), 'keep');
  await writeFile(path.join(home, '.runtime-install.lock'), 'other installation');
  await assert.rejects(installRuntime(home), /Another installation/);
  assert.equal(await readFile(path.join(home, '.runtime-install.lock'), 'utf8'), 'other installation');
});

test('inactive deployment semantics are checked before installation', async t => {
  const { home, staged } = await fixture(t);
  await writeFile(path.join(home, 'deployment.json'), JSON.stringify({ ...staged, status: 'running', activation_allowed: true }));
  let called = false;
  await assert.rejects(installRuntime(home, { runner: async () => { called = true; } }), /inactive staged/);
  assert.equal(called, false);
});

test('failed publication removes the temporary installation record', async t => {
  const { home } = await fixture(t);
  await mkdir(path.join(home, 'runtime/installed.json'));
  const runner = async file => ({ stdout: file === 'uv' ? '' : 'INSTALLED_RUNTIME_OK' });
  await assert.rejects(installRuntime(home, { runner }), /installation failed/);
  assert.ok(!(await readdir(path.join(home, 'runtime'))).some(name => name.startsWith('.installed-')));
  assert.deepEqual(await readdir(path.join(home, 'environments')), []);
});

test('bare running output is not live supervisor evidence', async t => {
  const { home, staged } = await fixture(t);
  const bin = path.join(home, 'environments', staged.runtime_manifest_digest.slice(7), 'bin');
  await mkdir(bin, { recursive: true });
  await writeFile(path.join(bin, 'python'), '#!/usr/bin/env node\nconsole.log(JSON.stringify({status:"running"}));\n', { mode: 0o700 });
  await writeFile(path.join(home, 'runtime/installed.json'), JSON.stringify({ schema_version: 1, runtime_manifest_digest: staged.runtime_manifest_digest, status: 'installed_not_running', activation_allowed: false }));
  await assert.rejects(runtimeCommand('status', home), /invalid control evidence/);
});

test('start forwards explicit host-bound file config into the installed runtime', async t => {
  const { home, staged } = await fixture(t);
  const digest = staged.runtime_manifest_digest.slice(7);
  const bin = path.join(home, 'environments', digest, 'bin');
  await mkdir(bin, { recursive: true });
  const python = path.join(bin, 'python');
  await writeFile(python, '#!/usr/bin/env node\nif (!process.argv.includes("--file-config") || !process.argv.includes(process.env.EXPECT_FILE_CONFIG)) process.exit(9);\nconsole.log(JSON.stringify({format:"puddingknowledge-local-supervisor/v1",status:"stopped"}));\n', { mode: 0o700 });
  await writeFile(path.join(home, 'runtime/installed.json'), JSON.stringify({ schema_version: 1, runtime_manifest_digest: staged.runtime_manifest_digest, status: 'installed_not_running', activation_allowed: false }));
  const fileConfig = path.join(home, 'file-config.json');
  await writeFile(fileConfig, JSON.stringify({ version: 1, bindings: [], parsers: [{ id: 'native' }], collection_id: 'uploaded_files' }));
  process.env.EXPECT_FILE_CONFIG = fileConfig;
  t.after(() => { delete process.env.EXPECT_FILE_CONFIG; });
  const observed = await runtimeCommand('start', home, { catalog: path.join(home, 'catalog.db'), wiki_root: path.join(home, 'wiki'), port: 19001, file_config: fileConfig });
  assert.equal(observed.status, 'stopped');
});
