/**
 * Social routes - thin proxy to Python /api/social/* (Postiz publishing
 * lane + approval queue). No business logic here; assembly lives in
 * .claude/scripts/social/dashboard_ops.py.
 *
 * Connect-URL hygiene: the OAuth URL rides the response body only - this
 * proxy never logs response bodies.
 */

import { Hono } from 'hono';
import { authedFetchJson } from '../framework-client.js';
import { inboundPersonaId, outboundPersonaId } from '../translate.js';

void inboundPersonaId;
void outboundPersonaId;

export const socialRoute = new Hono();

socialRoute.get('/api/social/status', async (c) => {
  const result = await authedFetchJson('/api/social/status');
  return c.json(result.json as Record<string, unknown>, result.status as 200);
});

socialRoute.get('/api/social/channels', async (c) => {
  const result = await authedFetchJson('/api/social/channels');
  return c.json(result.json as Record<string, unknown>, result.status as 200);
});

socialRoute.get('/api/social/queue', async (c) => {
  const search = new URL(c.req.url).search;
  const result = await authedFetchJson(`/api/social/queue${search}`);
  return c.json(result.json as Record<string, unknown>, result.status as 200);
});

socialRoute.get('/api/social/posts', async (c) => {
  const search = new URL(c.req.url).search;
  const result = await authedFetchJson(`/api/social/posts${search}`);
  return c.json(result.json as Record<string, unknown>, result.status as 200);
});

socialRoute.get('/api/social/connect-url', async (c) => {
  const search = new URL(c.req.url).search;
  const result = await authedFetchJson(`/api/social/connect-url${search}`);
  return c.json(result.json as Record<string, unknown>, result.status as 200);
});

socialRoute.post('/api/social/compose', async (c) => {
  const raw = await c.req.text();
  const result = await authedFetchJson('/api/social/compose', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: raw || '{}',
  });
  return c.json(result.json as Record<string, unknown>, result.status as 200);
});

socialRoute.post('/api/social/approve', async (c) => {
  const raw = await c.req.text();
  const result = await authedFetchJson('/api/social/approve', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: raw || '{}',
  });
  return c.json(result.json as Record<string, unknown>, result.status as 200);
});

socialRoute.post('/api/social/reject', async (c) => {
  const raw = await c.req.text();
  const result = await authedFetchJson('/api/social/reject', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: raw || '{}',
  });
  return c.json(result.json as Record<string, unknown>, result.status as 200);
});

socialRoute.post('/api/social/reconcile', async (c) => {
  const result = await authedFetchJson('/api/social/reconcile', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: '{}',
  });
  return c.json(result.json as Record<string, unknown>, result.status as 200);
});
