/**
 * csrf.test.ts — origin allowlist on mutation requests.
 */

import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { Hono } from 'hono';
import { buildCsrfMiddleware } from '../middleware/csrf.js';
import { _resetAuthPolicyForTest } from '../auth-policy.js';

describe('csrf: origin allowlist', () => {
  beforeEach(() => {
    _resetAuthPolicyForTest();
  });
  afterEach(() => {
    delete process.env.DASHBOARD_URL;
  });

  it('allows GET regardless of origin', async () => {
    const app = new Hono();
    app.use('*', buildCsrfMiddleware());
    app.get('/api/info', (c) => c.json({ ok: true }));
    const resp = await app.request('/api/info', {
      headers: { Origin: 'https://attacker.example.com' },
    });
    expect(resp.status).toBe(200);
  });

  it('allows mutation from localhost / 127.0.0.1', async () => {
    const app = new Hono();
    app.use('*', buildCsrfMiddleware());
    app.post('/api/agents', (c) => c.json({ ok: true }));

    const a = await app.request('/api/agents', {
      method: 'POST',
      headers: { Origin: 'http://localhost:5173' },
    });
    expect(a.status).toBe(200);

    const b = await app.request('/api/agents', {
      method: 'POST',
      headers: { Origin: 'http://127.0.0.1:3141' },
    });
    expect(b.status).toBe(200);
  });

  it('rejects mutation from cross-origin host not on allowlist', async () => {
    const app = new Hono();
    app.use('*', buildCsrfMiddleware());
    app.post('/api/agents', (c) => c.json({ ok: true }));

    const resp = await app.request('/api/agents', {
      method: 'POST',
      headers: { Origin: 'https://attacker.example.com' },
    });
    expect(resp.status).toBe(403);
    const body = (await resp.json()) as { error: string };
    expect(body.error).toMatch(/cross-origin/i);
  });
});
