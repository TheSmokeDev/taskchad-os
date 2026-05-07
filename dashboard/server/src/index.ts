/**
 * Server entry point — boots the Hono dashboard server.
 *
 * Boot sequence (R5 Minor 3 + R4 NM1):
 *   1. Resolve bind + port from env (DASHBOARD_BIND default 127.0.0.1,
 *      DASHBOARD_PORT default 3141).
 *   2. Resolve auth policy via the 4-branch token decision (resolveAuthPolicy).
 *      - Both equal → start.
 *      - Both unequal → exit 1.
 *      - Exactly one set → alias.
 *      - Neither set + non-loopback → exit 1.
 *      - Neither set + loopback + no DASHBOARD_DEV_MODE_NO_AUTH → exit 1.
 *      - Neither set + loopback + DASHBOARD_DEV_MODE_NO_AUTH=true → start with WARN.
 *   3. Capture policy in module-scope AUTH_POLICY (auth-policy.ts setAuthPolicy).
 *      Subsequent process.env mutation does NOT change request-time auth.
 *   4. Build the Hono app via buildDashboardApp().
 *   5. Bind via @hono/node-server `serve()`.
 */

import { serve } from '@hono/node-server';
import { buildDashboardApp } from './app.js';
import { resolveAuthPolicy, setAuthPolicy } from './auth-policy.js';
import { logger } from './logger.js';

function resolveBind(): string {
  const raw = (process.env.DASHBOARD_BIND ?? '').trim();
  return raw || '127.0.0.1';
}

function resolvePort(): number {
  const raw = (process.env.DASHBOARD_PORT ?? '').trim();
  if (!raw) return 3141;
  const n = Number.parseInt(raw, 10);
  if (Number.isNaN(n) || n <= 0 || n > 65535) {
    throw new Error(`invalid DASHBOARD_PORT: ${raw}`);
  }
  return n;
}

export interface BootResult {
  ok: boolean;
  error?: string;
  port?: number;
  bind?: string;
}

/**
 * Boot the server. Returns a BootResult — does NOT call process.exit
 * directly so vitest can drive the boot path without killing the test
 * process.
 */
export function boot(): BootResult {
  const bind = resolveBind();
  let port: number;
  try {
    port = resolvePort();
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    return { ok: false, error: msg };
  }

  const { policy, error } = resolveAuthPolicy({
    dashboardToken: process.env.DASHBOARD_TOKEN,
    orchestrationApiToken: process.env.ORCHESTRATION_API_TOKEN,
    devModeNoAuth: process.env.DASHBOARD_DEV_MODE_NO_AUTH,
    bind,
  });

  if (error || !policy) {
    return { ok: false, error: error ?? 'unknown auth policy error', bind, port };
  }

  setAuthPolicy(policy);

  if (policy.mode === 'dev-mode-loopback') {
    logger.warn(
      { bind, port },
      'DASHBOARD_DEV_MODE_NO_AUTH=true — starting with NO authentication on loopback. ' +
        'Every request will emit a WARN log line. Do NOT use this configuration in production.',
    );
  } else {
    logger.info(
      { bind, port, mode: policy.mode },
      'auth policy resolved',
    );
  }

  const app = buildDashboardApp();
  serve({
    fetch: app.fetch,
    hostname: bind,
    port,
  });

  logger.info({ bind, port }, 'dashboard server listening');

  return { ok: true, bind, port };
}

// Only boot when run as the main module (not when imported by tests).
const isMainModule = (() => {
  // Node ESM doesn't expose require.main; use process.argv heuristic.
  if (process.argv[1]) {
    const arg = process.argv[1];
    return arg.endsWith('index.ts') || arg.endsWith('index.js');
  }
  return false;
})();

if (isMainModule) {
  const result = boot();
  if (!result.ok) {
    logger.fatal({ error: result.error }, 'boot failed');
    // eslint-disable-next-line no-process-exit
    process.exit(1);
  }
}
