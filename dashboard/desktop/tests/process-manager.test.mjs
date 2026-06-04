import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { mkdirSync, mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const { ConfigStore, normalizeConfig } = require('../lib/config-store.cjs');
const { DesktopStackManager, buildStackCommands } = require('../lib/process-manager.cjs');
const { resolveDesktopPaths, resolveRepoRoot } = require('../lib/desktop-paths.cjs');

test('normalizes config without secrets', () => {
  const config = normalizeConfig({
    apiPort: '45123',
    dashboardPort: '33141',
    bind: '127.0.0.1',
    startPath: 'bad',
    autoStart: false,
    token: 'must-not-survive',
  });

  assert.equal(config.apiPort, 45123);
  assert.equal(config.dashboardPort, 33141);
  assert.equal(config.startPath, '/teams');
  assert.equal(config.autoStart, false);
  assert.equal(Object.hasOwn(config, 'token'), false);
});

test('config store writes only local desktop config', () => {
  const dir = mkdtempSync(path.join(tmpdir(), 'homie-desktop-config-'));
  try {
    const store = new ConfigStore(dir);
    const saved = store.save({ apiPort: 45123, dashboardPort: 33141, startPath: '/capabilities' });
    assert.deepEqual(store.load(), saved);
    assert.equal(store.load().startPath, '/capabilities');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('config store can use shell-provided first-run defaults', () => {
  const dir = mkdtempSync(path.join(tmpdir(), 'homie-desktop-env-config-'));
  try {
    const store = new ConfigStore(dir, {
      apiPort: 45123,
      dashboardPort: 33141,
      bind: '127.0.0.1',
      startPath: '/capabilities',
      autoStart: true,
    });
    const config = store.load();
    assert.equal(config.apiPort, 45123);
    assert.equal(config.dashboardPort, 33141);
    assert.equal(config.startPath, '/capabilities');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('builds desktop stack commands over Python API and Hono/static dashboard', () => {
  const commands = buildStackCommands({ apiPort: 45123, dashboardPort: 33141 }, {
    scriptsDir: 'scripts',
    serverDir: 'server',
    webDistDir: 'web-dist',
  });

  assert.deepEqual(commands.map((command) => command.name), ['python-api', 'hono-dashboard']);
  assert.equal(commands[0].env.ORCHESTRATION_API_PORT, '45123');
  assert.equal(commands[0].env.FRAMEWORK_API_URL, 'http://127.0.0.1:45123');
  assert.equal(commands[1].env.DASHBOARD_PORT, '33141');
  assert.equal(commands[1].env.FRAMEWORK_API_URL, 'http://127.0.0.1:45123');
  assert.equal(commands[1].env.DASHBOARD_STATIC_DIR, 'web-dist');
});

test('resolves package-aware repo root and bundled web assets', () => {
  const dir = mkdtempSync(path.join(tmpdir(), 'homie-desktop-paths-'));
  const repoRoot = path.join(dir, 'repo');
  const webDistDir = path.join(dir, 'resources', 'dashboard-web');
  const previousRepoRoot = process.env.HOMIE_REPO_ROOT;
  const previousWebDist = process.env.HOMIE_DESKTOP_WEB_DIST_DIR;
  try {
    mkdirSync(path.join(repoRoot, '.claude', 'scripts'), { recursive: true });
    mkdirSync(path.join(repoRoot, 'dashboard', 'server'), { recursive: true });
    mkdirSync(path.join(repoRoot, 'dashboard', 'web'), { recursive: true });
    mkdirSync(webDistDir, { recursive: true });
    process.env.HOMIE_REPO_ROOT = repoRoot;
    process.env.HOMIE_DESKTOP_WEB_DIST_DIR = webDistDir;

    assert.equal(resolveRepoRoot(path.join(dir, 'missing')), repoRoot);
    const paths = resolveDesktopPaths();
    assert.equal(paths.root, repoRoot);
    assert.equal(paths.webDistDir, webDistDir);
  } finally {
    if (previousRepoRoot === undefined) {
      delete process.env.HOMIE_REPO_ROOT;
    } else {
      process.env.HOMIE_REPO_ROOT = previousRepoRoot;
    }
    if (previousWebDist === undefined) {
      delete process.env.HOMIE_DESKTOP_WEB_DIST_DIR;
    } else {
      process.env.HOMIE_DESKTOP_WEB_DIST_DIR = previousWebDist;
    }
    rmSync(dir, { recursive: true, force: true });
  }
});

test('manager reports target url and bounded service status', async () => {
  const children = [];
  const spawnOptions = [];
  const fakeSpawn = (command, args, options) => {
    const child = new EventEmitter();
    child.command = command;
    child.args = args;
    spawnOptions.push(options);
    child.pid = 1000 + children.length;
    child.exitCode = null;
    child.stdout = new EventEmitter();
    child.stderr = new EventEmitter();
    child.kill = () => {
      child.exitCode = 0;
      child.emit('exit', 0, null);
    };
    children.push(child);
    return child;
  };

  const manager = new DesktopStackManager(
    { apiPort: 45123, dashboardPort: 33141, startPath: '/teams' },
    {
      spawnFn: fakeSpawn,
      paths: { scriptsDir: 'scripts', serverDir: 'server', webDistDir: 'web-dist' },
    },
  );

  const started = await manager.start();
  assert.equal(started.running, true);
  assert.equal(started.targetUrl, 'http://127.0.0.1:33141/teams');
  assert.deepEqual(started.services.map((service) => service.name), ['python-api', 'hono-dashboard']);
  assert.equal(children.length, 2);
  assert.equal(spawnOptions.every((options) => options.windowsHide === true), true);
  if (process.platform === 'win32') {
    assert.equal(spawnOptions.every((options) => options.shell === true), true);
  }

  children[0].stdout.emit('data', 'ready\n');
  assert.ok(manager.status().logs.some((line) => line.message === 'ready'));
});
