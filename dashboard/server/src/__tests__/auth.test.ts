/**
 * auth.test.ts — 11 cases per PRP §1246 R4 NM1.
 *
 * Tests the resolveAuthPolicy() pure function (covers branches a-d) and
 * the buildAuthMiddleware() request behavior under each policy mode.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { resolveAuthPolicy, setAuthPolicy, _resetAuthPolicyForTest } from '../auth-policy.js';
import { buildAuthMiddleware } from '../middleware/auth.js';
import { Hono } from 'hono';

describe('auth: 4-branch boot policy (R4 NM1)', () => {
  it('rejects missing/wrong token', async () => {
    _resetAuthPolicyForTest();
    setAuthPolicy({
      mode: 'token-equal',
      expectedToken: 'correct-token',
      warnPerRequest: false,
      bind: '127.0.0.1',
    });
    const app = new Hono();
    app.use('*', buildAuthMiddleware());
    app.get('/api/info', (c) => c.json({ ok: true }));

    const noAuth = await app.request('/api/info');
    expect(noAuth.status).toBe(401);

    const wrong = await app.request('/api/info', {
      headers: { Authorization: 'Bearer wrong-token' },
    });
    expect(wrong.status).toBe(401);
  });

  it('accepts correct token via Bearer header', async () => {
    _resetAuthPolicyForTest();
    setAuthPolicy({
      mode: 'token-equal',
      expectedToken: 'correct-token',
      warnPerRequest: false,
      bind: '127.0.0.1',
    });
    const app = new Hono();
    app.use('*', buildAuthMiddleware());
    app.get('/api/info', (c) => c.json({ ok: true }));

    const ok = await app.request('/api/info', {
      headers: { Authorization: 'Bearer correct-token' },
    });
    expect(ok.status).toBe(200);
  });

  it('SSE token via query: accepted on /api/conversation/.../stream', async () => {
    _resetAuthPolicyForTest();
    setAuthPolicy({
      mode: 'token-equal',
      expectedToken: 'sse-token',
      warnPerRequest: false,
      bind: '127.0.0.1',
    });
    const app = new Hono();
    app.use('*', buildAuthMiddleware());
    app.get('/api/conversation/:id/stream', (c) => c.json({ ok: true }));
    app.get('/api/info', (c) => c.json({ ok: true }));

    const ok = await app.request('/api/conversation/main/stream?token=sse-token');
    expect(ok.status).toBe(200);

    // Same query token rejected on a non-SSE endpoint.
    const reject = await app.request('/api/info?token=sse-token');
    expect(reject.status).toBe(401);
  });

  it('query token is accepted for Cabinet voice document/static GETs only', async () => {
    _resetAuthPolicyForTest();
    setAuthPolicy({
      mode: 'token-equal',
      expectedToken: 'voice-token',
      warnPerRequest: false,
      bind: '127.0.0.1',
    });
    const app = new Hono();
    app.use('*', buildAuthMiddleware());
    app.get('/api/cabinet/voice/ui', (c) => c.json({ ok: true }));
    app.get('/api/cabinet/voice/client.bundle.js', (c) => c.text('js'));
    app.get('/api/cabinet/voice/avatars/:id.png', (c) => c.text('png'));
    app.post('/api/cabinet/voice/ui', (c) => c.json({ ok: true }));

    expect((await app.request('/api/cabinet/voice/ui?token=voice-token')).status).toBe(200);
    expect((await app.request('/api/cabinet/voice/client.bundle.js?token=voice-token')).status).toBe(200);
    expect((await app.request('/api/cabinet/voice/avatars/main.png?token=voice-token')).status).toBe(200);
    expect((await app.request('/api/cabinet/voice/ui?token=wrong')).status).toBe(401);
    expect((await app.request('/api/cabinet/voice/ui?token=voice-token', { method: 'POST' })).status).toBe(401);
  });

  it('token-to-Bearer translation: query token must match expectedToken', async () => {
    _resetAuthPolicyForTest();
    setAuthPolicy({
      mode: 'token-equal',
      expectedToken: 'expected',
      warnPerRequest: false,
      bind: '127.0.0.1',
    });
    const app = new Hono();
    app.use('*', buildAuthMiddleware());
    app.get('/api/conversation/:id/stream', (c) => c.json({ ok: true }));

    const wrong = await app.request('/api/conversation/main/stream?token=different');
    expect(wrong.status).toBe(401);
  });

  it('refuses-to-start when both tokens differ', () => {
    const r = resolveAuthPolicy({
      dashboardToken: 'a',
      orchestrationApiToken: 'b',
      devModeNoAuth: undefined,
      bind: '127.0.0.1',
    });
    expect(r.policy).toBeNull();
    expect(r.error).toMatch(/both set but DIFFER/i);
  });

  it('starts when both equal', () => {
    const r = resolveAuthPolicy({
      dashboardToken: 'same',
      orchestrationApiToken: 'same',
      devModeNoAuth: undefined,
      bind: '127.0.0.1',
    });
    expect(r.policy?.mode).toBe('token-equal');
    expect(r.policy?.expectedToken).toBe('same');
    expect(r.policy?.warnPerRequest).toBe(false);
  });

  it('starts when only one set (alias)', () => {
    const a = resolveAuthPolicy({
      dashboardToken: 'only-dash',
      orchestrationApiToken: undefined,
      devModeNoAuth: undefined,
      bind: '127.0.0.1',
    });
    expect(a.policy?.mode).toBe('token-alias');
    expect(a.policy?.expectedToken).toBe('only-dash');

    const b = resolveAuthPolicy({
      dashboardToken: undefined,
      orchestrationApiToken: 'only-orch',
      devModeNoAuth: undefined,
      bind: '127.0.0.1',
    });
    expect(b.policy?.mode).toBe('token-alias');
    expect(b.policy?.expectedToken).toBe('only-orch');
  });

  it('refuses-to-start when neither set + bind non-loopback', () => {
    const r = resolveAuthPolicy({
      dashboardToken: undefined,
      orchestrationApiToken: undefined,
      devModeNoAuth: undefined,
      bind: '0.0.0.0',
    });
    expect(r.policy).toBeNull();
    expect(r.error).toMatch(/non-loopback/i);
  });

  it('refuses-to-start when neither set + bind loopback + no DASHBOARD_DEV_MODE_NO_AUTH', () => {
    const r = resolveAuthPolicy({
      dashboardToken: undefined,
      orchestrationApiToken: undefined,
      devModeNoAuth: undefined,
      bind: '127.0.0.1',
    });
    expect(r.policy).toBeNull();
    expect(r.error).toMatch(/loopback bind alone does NOT enable no-auth/i);
  });

  it('starts-with-warning when DASHBOARD_DEV_MODE_NO_AUTH=true on loopback', async () => {
    const r = resolveAuthPolicy({
      dashboardToken: undefined,
      orchestrationApiToken: undefined,
      devModeNoAuth: 'true',
      bind: '127.0.0.1',
    });
    expect(r.policy?.mode).toBe('dev-mode-loopback');
    expect(r.policy?.warnPerRequest).toBe(true);
    expect(r.policy?.expectedToken).toBeNull();

    // And the middleware lets requests through without auth.
    _resetAuthPolicyForTest();
    setAuthPolicy(r.policy!);
    const app = new Hono();
    app.use('*', buildAuthMiddleware());
    app.get('/api/info', (c) => c.json({ ok: true }));
    const resp = await app.request('/api/info');
    expect(resp.status).toBe(200);
  });

  it('boot-snapshot is immune to runtime env mutation (R5 Minor 3)', async () => {
    // Capture policy at "boot" with a specific token.
    const r = resolveAuthPolicy({
      dashboardToken: 'boot-token',
      orchestrationApiToken: undefined,
      devModeNoAuth: undefined,
      bind: '127.0.0.1',
    });
    _resetAuthPolicyForTest();
    setAuthPolicy(r.policy!);

    const app = new Hono();
    app.use('*', buildAuthMiddleware());
    app.get('/api/info', (c) => c.json({ ok: true }));

    // Mutate process.env AFTER boot — must NOT affect auth.
    vi.stubEnv('DASHBOARD_TOKEN', 'changed-after-boot');
    try {
      // Old token still works (matches AUTH_POLICY snapshot).
      const okOld = await app.request('/api/info', {
        headers: { Authorization: 'Bearer boot-token' },
      });
      expect(okOld.status).toBe(200);
      // New token does NOT work.
      const rejectNew = await app.request('/api/info', {
        headers: { Authorization: 'Bearer changed-after-boot' },
      });
      expect(rejectNew.status).toBe(401);
    } finally {
      vi.unstubAllEnvs();
    }
  });
});

describe('auth: /api/health is exempt', () => {
  beforeEach(() => {
    _resetAuthPolicyForTest();
    setAuthPolicy({
      mode: 'token-equal',
      expectedToken: 'token',
      warnPerRequest: false,
      bind: '127.0.0.1',
    });
  });
  afterEach(() => {
    _resetAuthPolicyForTest();
  });

  it('/api/health passes through unauthenticated', async () => {
    const app = new Hono();
    app.use('*', buildAuthMiddleware());
    app.get('/api/health', (c) => c.json({ status: 'ok' }));
    const resp = await app.request('/api/health');
    expect(resp.status).toBe(200);
  });
});
