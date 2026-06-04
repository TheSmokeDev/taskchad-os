import { mkdtempSync, rmSync, writeFileSync, mkdirSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { afterEach, describe, expect, it } from 'vitest';
import { buildDashboardApp } from '../app.js';
import { _resetAuthPolicyForTest, setAuthPolicy } from '../auth-policy.js';

let staticDir: string | null = null;
const previousStaticDir = process.env.DASHBOARD_STATIC_DIR;

afterEach(() => {
  _resetAuthPolicyForTest();
  if (staticDir) {
    rmSync(staticDir, { recursive: true, force: true });
    staticDir = null;
  }
  if (previousStaticDir === undefined) {
    delete process.env.DASHBOARD_STATIC_DIR;
  } else {
    process.env.DASHBOARD_STATIC_DIR = previousStaticDir;
  }
});

function makeStaticDir(): string {
  _resetAuthPolicyForTest();
  setAuthPolicy({
    mode: 'dev-mode-loopback',
    warnPerRequest: false,
    bind: '127.0.0.1',
  });
  staticDir = mkdtempSync(path.join(tmpdir(), 'homie-dashboard-static-'));
  mkdirSync(path.join(staticDir, 'assets'));
  writeFileSync(path.join(staticDir, 'index.html'), '<div id="app">Operating Room</div>');
  writeFileSync(path.join(staticDir, 'assets', 'app.js'), 'console.log("homie")');
  process.env.DASHBOARD_STATIC_DIR = staticDir;
  return staticDir;
}

describe('dashboard static web fallback', () => {
  it('serves assets and falls back app routes to index.html', async () => {
    makeStaticDir();
    const app = buildDashboardApp();

    const teams = await app.request('/teams');
    expect(teams.status).toBe(200);
    expect(await teams.text()).toContain('Operating Room');

    const asset = await app.request('/assets/app.js');
    expect(asset.status).toBe(200);
    expect(asset.headers.get('content-type')).toContain('text/javascript');
  });

  it('keeps unknown API paths as API 404s', async () => {
    makeStaticDir();
    const app = buildDashboardApp();

    const response = await app.request('/api/not-real');
    expect(response.status).toBe(404);
    expect(await response.text()).not.toContain('Operating Room');
  });
});
