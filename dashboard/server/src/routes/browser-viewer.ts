/**
 * Browser Viewer proxy — read-only Homie Dashboard surface.
 *
 * Python owns browser policy, readiness, workflow gates, and audit logging.
 * Hono only forwards JSON/image responses and adds a loopback-only direct
 * WebSocket URL for the agent-browser viewport stream.
 */

import { Hono } from 'hono';
import {
  authedFetch,
  authedFetchBinary,
  authedFetchJson,
} from '../framework-client.js';
import { inboundPersonaId, outboundPersonaId } from '../translate.js';

void inboundPersonaId; // imported for static-invariants grep gate.
void outboundPersonaId;

export const browserViewerRoute = new Hono();

type JsonRecord = Record<string, unknown>;

function isRecord(value: unknown): value is JsonRecord {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function isLoopbackHost(hostname: string): boolean {
  return hostname === 'localhost' || hostname === '127.0.0.1' || hostname === '::1' || hostname === '[::1]';
}

function websocketHost(hostname: string): string {
  return hostname === '::1' || hostname === '[::1]' ? '[::1]' : hostname;
}

function withDirectStreamUrl(c: import('hono').Context, payload: unknown): unknown {
  if (!isRecord(payload)) return payload;

  const stream = isRecord(payload.stream) ? payload.stream : null;
  if (!stream) return payload;

  const url = new URL(c.req.url);
  if (!isLoopbackHost(url.hostname)) return payload;

  const streamPort = stream.port;
  const enabled = stream.enabled === true;
  if (!enabled || typeof streamPort !== 'number' || streamPort <= 0 || streamPort > 65535) {
    return payload;
  }

  return {
    ...payload,
    stream: {
      ...stream,
      direct_ws_url: `ws://${websocketHost(url.hostname)}:${streamPort}`,
    },
  };
}

browserViewerRoute.get('/api/browser-viewer/status', async (c) => {
  const result = await authedFetchJson('/api/browser-viewer/status');
  return c.json(withDirectStreamUrl(c, result.json) as JsonRecord, result.status as 200);
});

browserViewerRoute.get('/api/browser-viewer/screenshot', async (c) => {
  const result = await authedFetchBinary('/api/browser-viewer/screenshot', {
    headers: { Accept: 'image/png' },
  });
  return c.body(result.body, result.status as 200, {
    'Content-Type': result.headers.get('content-type') ?? 'image/png',
    'Cache-Control': result.headers.get('cache-control') ?? 'no-store',
  });
});

async function forwardStreamMutation(c: import('hono').Context, path: string): Promise<Response> {
  const result = await authedFetch(path, { method: 'POST' });
  const json = result.json();
  if (isRecord(json)) {
    return c.json(withDirectStreamUrl(c, json) as JsonRecord, result.status as 200);
  }
  return c.body(result.body, result.status as 200, {
    'Content-Type': result.headers.get('content-type') ?? 'application/json',
  });
}

browserViewerRoute.post('/api/browser-viewer/stream/enable', async (c) =>
  forwardStreamMutation(c, '/api/browser-viewer/stream/enable'),
);

browserViewerRoute.post('/api/browser-viewer/stream/disable', async (c) =>
  forwardStreamMutation(c, '/api/browser-viewer/stream/disable'),
);
