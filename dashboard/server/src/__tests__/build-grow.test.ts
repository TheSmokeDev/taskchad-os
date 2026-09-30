import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest';
import { Hono } from 'hono';
import { buildGrowRoute } from '../routes/build-grow.js';
import { buildAuthMiddleware } from '../middleware/auth.js';
import { setAuthPolicy, _resetAuthPolicyForTest } from '../auth-policy.js';

function app() {
  const instance = new Hono();
  instance.use('*', buildAuthMiddleware());
  instance.route('/', buildGrowRoute);
  return instance;
}

describe('Build + Grow proxy identity and artifact boundary', () => {
  beforeEach(() => {
    _resetAuthPolicyForTest();
    setAuthPolicy({ mode: 'token-equal', expectedToken: 'operator-token', warnPerRequest: false, bind: '127.0.0.1' });
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ projects: [] }), { status: 200 })));
  });
  afterEach(() => { vi.unstubAllGlobals(); _resetAuthPolicyForTest(); });

  it('preserves existing operator auth and never accepts a URL token', async () => {
    const instance = app();
    expect((await instance.request('/api/build-grow/projects')).status).toBe(401);
    expect((await instance.request('/api/build-grow/projects?token=operator-token')).status).toBe(401);
    expect((await instance.request('/api/build-grow/projects', { headers: { Authorization: 'Bearer customer-token' } })).status).toBe(401);
    expect(fetch).not.toHaveBeenCalled();
  });

  it('forwards caller Bearer without leaking query parameters', async () => {
    await app().request('/api/build-grow/projects?token=do-not-forward', { headers: { Authorization: 'Bearer operator-token' } });
    expect(fetch).toHaveBeenCalledWith('http://127.0.0.1:4322/api/build-grow/projects', expect.objectContaining({ headers: { Authorization: 'Bearer operator-token', Accept: 'application/json' } }));
  });

  it('passes mutation bytes and Python conflict status without reinterpreting the review', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(new Response(JSON.stringify({ detail: 'stale brief revision' }), { status: 409 }));
    const body = JSON.stringify({ decision: 'approved', expected_sha256: 'abc', note: 'Ready' });
    const response = await app().request('/api/build-grow/deliverables/7/review', {
      method: 'POST', headers: { Authorization: 'Bearer operator-token', 'content-type': 'application/json' }, body,
    });
    expect(response.status).toBe(409);
    expect(await response.json()).toEqual({ detail: 'stale brief revision' });
    expect(fetch).toHaveBeenCalledWith('http://127.0.0.1:4322/api/build-grow/deliverables/7/review', expect.objectContaining({ method: 'POST', body }));
  });

  it('relays exact artifact bytes with attachment, digest, and no-store headers', async () => {
    const bytes = new Uint8Array([80, 75, 3, 4, 0, 255]);
    vi.mocked(fetch).mockResolvedValueOnce(new Response(bytes, {
      headers: { 'content-type': 'application/zip', 'content-disposition': 'attachment; filename="website.zip"', ETag: '"sha256:abc"', 'x-content-type-options': 'nosniff' },
    }));
    const response = await app().request('/api/build-grow/deliverables/7/download', { headers: { Authorization: 'Bearer operator-token' } });
    expect(new Uint8Array(await response.arrayBuffer())).toEqual(bytes);
    expect(response.headers.get('content-disposition')).toBe('attachment; filename="website.zip"');
    expect(response.headers.get('etag')).toBe('"sha256:abc"');
    expect(response.headers.get('cache-control')).toBe('no-store');
    expect(response.headers.get('x-content-type-options')).toBe('nosniff');
  });

  it('passes an empty caller override in explicit loopback development', async () => {
    _resetAuthPolicyForTest();
    setAuthPolicy({ mode: 'dev-mode-loopback', expectedToken: null, warnPerRequest: true, bind: '127.0.0.1' });
    await app().request('/api/build-grow/projects');
    const headers = vi.mocked(fetch).mock.calls[0]?.[1]?.headers as Record<string, string>;
    expect(headers.Authorization).toBeUndefined();
  });
});
