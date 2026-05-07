/**
 * response-headers.test.ts — security headers always present.
 */

import { describe, expect, it } from 'vitest';
import { Hono } from 'hono';
import { buildResponseHeadersMiddleware } from '../middleware/response-headers.js';

describe('response headers', () => {
  it('sets X-Content-Type-Options: nosniff', async () => {
    const app = new Hono();
    app.use('*', buildResponseHeadersMiddleware());
    app.get('/api/info', (c) => c.json({ ok: true }));
    const resp = await app.request('/api/info');
    expect(resp.headers.get('x-content-type-options')).toBe('nosniff');
  });

  it('sets X-Frame-Options: DENY', async () => {
    const app = new Hono();
    app.use('*', buildResponseHeadersMiddleware());
    app.get('/api/info', (c) => c.json({ ok: true }));
    const resp = await app.request('/api/info');
    expect(resp.headers.get('x-frame-options')).toBe('DENY');
  });

  it('sets Referrer-Policy: no-referrer', async () => {
    const app = new Hono();
    app.use('*', buildResponseHeadersMiddleware());
    app.get('/api/info', (c) => c.json({ ok: true }));
    const resp = await app.request('/api/info');
    expect(resp.headers.get('referrer-policy')).toBe('no-referrer');
  });

  it('sets Cache-Control: no-store on /api/* by default', async () => {
    const app = new Hono();
    app.use('*', buildResponseHeadersMiddleware());
    app.get('/api/info', (c) => c.json({ ok: true }));
    const resp = await app.request('/api/info');
    expect(resp.headers.get('cache-control')).toBe('no-store');
  });
});
