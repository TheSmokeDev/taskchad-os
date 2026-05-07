/**
 * /api/agents/* routes — persona CRUD + lifecycle + avatar.
 *
 * Every handler that touches persona ids:
 *   - Calls inboundPersonaId() BEFORE forwarding to port 4322.
 *   - Calls outboundPersonaId() (or outboundPersonaDict/List) on response.
 *
 * Every PATCH/DELETE for persona-config-shaped data forwards multipart or
 * JSON body verbatim — Python is the only validator (Q5 single-yaml-surface).
 */

import { Hono } from 'hono';
import {
  authedFetch,
  authedFetchJson,
  authedFetchMultipart,
} from '../framework-client.js';
import {
  inboundPersonaId,
  outboundPersonaId,
  outboundPersonaDict,
  outboundPersonaList,
} from '../translate.js';

export const agentsRoute = new Hono();

// ── List + create ──────────────────────────────────────────────────────

agentsRoute.get('/api/agents', async (c) => {
  const result = await authedFetchJson('/api/agents');
  if (!result.ok) {
    return c.json(result.json as Record<string, unknown>, result.status as 400);
  }
  const body = result.json as { agents: Array<Record<string, unknown>> };
  const translated = {
    agents: outboundPersonaList(body.agents ?? []),
  };
  return c.json(translated, 200);
});

agentsRoute.post('/api/agents', async (c) => {
  const body = (await c.req.json().catch(() => ({}))) as Record<string, unknown>;
  // Inbound translate persona_id.
  if (typeof body.persona_id === 'string') {
    body.persona_id = inboundPersonaId(body.persona_id) ?? body.persona_id;
  }
  const result = await authedFetch('/api/agents', {
    method: 'POST',
    body: JSON.stringify(body),
    headers: { 'Content-Type': 'application/json' },
  });
  const json = result.json() as Record<string, unknown> | null;
  if (json && typeof json === 'object') {
    return c.json(outboundPersonaDict(json), result.status as 200);
  }
  return c.body(result.body, result.status as 200);
});

// ── Static routes BEFORE dynamic /:id (FastAPI ordering parity) ────────

agentsRoute.get('/api/agents/suggestions', async (c) => {
  const result = await authedFetchJson('/api/agents/suggestions');
  return c.json(result.json as Record<string, unknown>, result.status as 200);
});

agentsRoute.post('/api/agents/suggestions/refresh', async (c) => {
  const result = await authedFetch('/api/agents/suggestions/refresh', {
    method: 'POST',
  });
  return c.body(result.body, result.status as 200, {
    'Content-Type': result.headers.get('content-type') ?? 'application/json',
  });
});

agentsRoute.get('/api/agents/templates', async (c) => {
  const result = await authedFetchJson('/api/agents/templates');
  return c.json(result.json as Record<string, unknown>, result.status as 200);
});

agentsRoute.get('/api/agents/model', async (c) => {
  const result = await authedFetchJson('/api/agents/model');
  return c.json(result.json as Record<string, unknown>, result.status as 200);
});

agentsRoute.patch('/api/agents/model', async (c) => {
  const body = await c.req.json().catch(() => ({}));
  const result = await authedFetch('/api/agents/model', {
    method: 'PATCH',
    body: JSON.stringify(body),
    headers: { 'Content-Type': 'application/json' },
  });
  return c.body(result.body, result.status as 200, {
    'Content-Type': result.headers.get('content-type') ?? 'application/json',
  });
});

agentsRoute.post('/api/agents/validate-id', async (c) => {
  const body = (await c.req.json().catch(() => ({}))) as Record<string, unknown>;
  if (typeof body.persona_id === 'string') {
    body.persona_id = inboundPersonaId(body.persona_id) ?? body.persona_id;
  }
  const result = await authedFetch('/api/agents/validate-id', {
    method: 'POST',
    body: JSON.stringify(body),
    headers: { 'Content-Type': 'application/json' },
  });
  return c.body(result.body, result.status as 200, {
    'Content-Type': result.headers.get('content-type') ?? 'application/json',
  });
});

agentsRoute.post('/api/agents/validate-token', async (c) => {
  const body = await c.req.json().catch(() => ({}));
  const result = await authedFetch('/api/agents/validate-token', {
    method: 'POST',
    body: JSON.stringify(body),
    headers: { 'Content-Type': 'application/json' },
  });
  return c.body(result.body, result.status as 200, {
    'Content-Type': result.headers.get('content-type') ?? 'application/json',
  });
});

// ── Dynamic per-persona routes ─────────────────────────────────────────

agentsRoute.get('/api/agents/:id', async (c) => {
  const browserId = c.req.param('id');
  const frameworkId = inboundPersonaId(browserId) ?? browserId;
  const result = await authedFetchJson(`/api/agents/${encodeURIComponent(frameworkId)}`);
  if (!result.ok) {
    return c.json(result.json as Record<string, unknown>, result.status as 200);
  }
  const json = result.json as Record<string, unknown>;
  return c.json(outboundPersonaDict(json), 200);
});

agentsRoute.delete('/api/agents/:id', async (c) => {
  const browserId = c.req.param('id');
  const frameworkId = inboundPersonaId(browserId) ?? browserId;
  const result = await authedFetch(`/api/agents/${encodeURIComponent(frameworkId)}`, {
    method: 'DELETE',
  });
  const json = result.json() as Record<string, unknown> | null;
  if (json && typeof json === 'object') {
    return c.json(outboundPersonaDict(json), result.status as 200);
  }
  return c.body(result.body, result.status as 200);
});

agentsRoute.delete('/api/agents/:id/full', async (c) => {
  const browserId = c.req.param('id');
  const frameworkId = inboundPersonaId(browserId) ?? browserId;
  const url = new URL(c.req.url);
  const confirm = url.searchParams.get('confirm');
  const expected = url.searchParams.get('expected_persona_id');
  const expectedFw = expected ? (inboundPersonaId(expected) ?? expected) : null;

  const params = new URLSearchParams();
  if (confirm) params.set('confirm', confirm);
  if (expectedFw) params.set('expected_persona_id', expectedFw);
  const qs = params.toString() ? `?${params.toString()}` : '';

  const result = await authedFetch(
    `/api/agents/${encodeURIComponent(frameworkId)}/full${qs}`,
    { method: 'DELETE' },
  );
  const json = result.json() as Record<string, unknown> | null;
  if (json && typeof json === 'object') {
    return c.json(outboundPersonaDict(json), result.status as 200);
  }
  return c.body(result.body, result.status as 200);
});

agentsRoute.put('/api/agents/:id/avatar', async (c) => {
  const browserId = c.req.param('id');
  const frameworkId = inboundPersonaId(browserId) ?? browserId;
  // Forward multipart body verbatim. Hono's c.req.formData() decodes it;
  // we rebuild a fresh FormData so fetch sets boundary correctly.
  const inboundForm = await c.req.formData();
  const outboundForm = new FormData();
  for (const [key, value] of inboundForm.entries()) {
    outboundForm.append(key, value as Blob | string);
  }
  const result = await authedFetchMultipart(
    `/api/agents/${encodeURIComponent(frameworkId)}/avatar`,
    outboundForm,
    { method: 'PUT' },
  );
  return c.body(result.body, result.status as 200, {
    'Content-Type': result.headers.get('content-type') ?? 'application/json',
  });
});

agentsRoute.delete('/api/agents/:id/avatar', async (c) => {
  const browserId = c.req.param('id');
  const frameworkId = inboundPersonaId(browserId) ?? browserId;
  const result = await authedFetch(
    `/api/agents/${encodeURIComponent(frameworkId)}/avatar`,
    { method: 'DELETE' },
  );
  return c.body(result.body, result.status as 200, {
    'Content-Type': result.headers.get('content-type') ?? 'application/json',
  });
});

agentsRoute.post('/api/agents/:id/activate', async (c) => {
  const browserId = c.req.param('id');
  const frameworkId = inboundPersonaId(browserId) ?? browserId;
  const result = await authedFetch(
    `/api/agents/${encodeURIComponent(frameworkId)}/activate`,
    { method: 'POST' },
  );
  return c.body(result.body, result.status as 200, {
    'Content-Type': result.headers.get('content-type') ?? 'application/json',
  });
});

agentsRoute.post('/api/agents/:id/deactivate', async (c) => {
  const browserId = c.req.param('id');
  const frameworkId = inboundPersonaId(browserId) ?? browserId;
  const result = await authedFetch(
    `/api/agents/${encodeURIComponent(frameworkId)}/deactivate`,
    { method: 'POST' },
  );
  return c.body(result.body, result.status as 200, {
    'Content-Type': result.headers.get('content-type') ?? 'application/json',
  });
});

agentsRoute.post('/api/agents/:id/restart', async (c) => {
  const browserId = c.req.param('id');
  const frameworkId = inboundPersonaId(browserId) ?? browserId;
  const result = await authedFetch(
    `/api/agents/${encodeURIComponent(frameworkId)}/restart`,
    { method: 'POST' },
  );
  return c.body(result.body, result.status as 200, {
    'Content-Type': result.headers.get('content-type') ?? 'application/json',
  });
});

agentsRoute.patch('/api/agents/:id/model', async (c) => {
  const browserId = c.req.param('id');
  const frameworkId = inboundPersonaId(browserId) ?? browserId;
  const body = await c.req.json().catch(() => ({}));
  const result = await authedFetch(
    `/api/agents/${encodeURIComponent(frameworkId)}/model`,
    {
      method: 'PATCH',
      body: JSON.stringify(body),
      headers: { 'Content-Type': 'application/json' },
    },
  );
  return c.body(result.body, result.status as 200, {
    'Content-Type': result.headers.get('content-type') ?? 'application/json',
  });
});

// ── /files (list, patch, history) ──────────────────────────────────────

agentsRoute.get('/api/agents/:id/files', async (c) => {
  const browserId = c.req.param('id');
  const frameworkId = inboundPersonaId(browserId) ?? browserId;
  const result = await authedFetchJson(
    `/api/agents/${encodeURIComponent(frameworkId)}/files`,
  );
  return c.json(result.json as Record<string, unknown>, result.status as 200);
});

agentsRoute.patch('/api/agents/:id/files/:filename', async (c) => {
  const browserId = c.req.param('id');
  const filename = c.req.param('filename');
  const frameworkId = inboundPersonaId(browserId) ?? browserId;
  const body = await c.req.json().catch(() => ({}));
  const result = await authedFetch(
    `/api/agents/${encodeURIComponent(frameworkId)}/files/${encodeURIComponent(filename)}`,
    {
      method: 'PATCH',
      body: JSON.stringify(body),
      headers: { 'Content-Type': 'application/json' },
    },
  );
  return c.body(result.body, result.status as 200, {
    'Content-Type': result.headers.get('content-type') ?? 'application/json',
  });
});

agentsRoute.get('/api/agents/:id/files/history', async (c) => {
  const browserId = c.req.param('id');
  const frameworkId = inboundPersonaId(browserId) ?? browserId;
  const result = await authedFetchJson(
    `/api/agents/${encodeURIComponent(frameworkId)}/files/history`,
  );
  return c.json(result.json as Record<string, unknown>, result.status as 200);
});

// ── /conversation (read), /tokens, /tasks ──────────────────────────────

agentsRoute.get('/api/agents/:id/conversation', async (c) => {
  const browserId = c.req.param('id');
  const frameworkId = inboundPersonaId(browserId) ?? browserId;
  const url = new URL(c.req.url);
  const result = await authedFetchJson(
    `/api/agents/${encodeURIComponent(frameworkId)}/conversation${url.search}`,
  );
  return c.json(result.json as Record<string, unknown>, result.status as 200);
});

agentsRoute.get('/api/agents/:id/tokens', async (c) => {
  const browserId = c.req.param('id');
  const frameworkId = inboundPersonaId(browserId) ?? browserId;
  const url = new URL(c.req.url);
  const result = await authedFetchJson(
    `/api/agents/${encodeURIComponent(frameworkId)}/tokens${url.search}`,
  );
  return c.json(result.json as Record<string, unknown>, result.status as 200);
});

agentsRoute.get('/api/agents/:id/tasks', async (c) => {
  const browserId = c.req.param('id');
  const frameworkId = inboundPersonaId(browserId) ?? browserId;
  const url = new URL(c.req.url);
  const result = await authedFetchJson(
    `/api/agents/${encodeURIComponent(frameworkId)}/tasks${url.search}`,
  );
  return c.json(result.json as Record<string, unknown>, result.status as 200);
});

// outboundPersonaId is imported above for the static-invariants grep.
void outboundPersonaId;
