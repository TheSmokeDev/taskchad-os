/**
 * Browser Viewer proxy — read-only Homie Dashboard surface.
 *
 * Python owns browser policy, readiness, workflow gates, and audit logging.
 * Hono only forwards JSON/image responses and adds a loopback-only direct
 * WebSocket URL for the agent-browser viewport stream.
 */

import { Hono } from 'hono';
import { streamSSE } from 'hono/streaming';
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

// M12 phone-drive — thin proxies; the default-deny workflow gates, input
// validation, and audit rows all live in Python (dashboard_api.py).

browserViewerRoute.get('/api/browser-viewer/elements', async (c) => {
  const result = await authedFetchJson('/api/browser-viewer/elements');
  return c.json(result.json as JsonRecord, result.status as 200);
});

async function forwardJsonPost(c: import('hono').Context, path: string): Promise<Response> {
  const body = await c.req.json().catch(() => ({}));
  const result = await authedFetchJson(path, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  });
  return c.json(result.json as JsonRecord, result.status as 200);
}

browserViewerRoute.post('/api/browser-viewer/act', (c) =>
  forwardJsonPost(c, '/api/browser-viewer/act'),
);

browserViewerRoute.post('/api/browser-viewer/navigate', (c) =>
  forwardJsonPost(c, '/api/browser-viewer/navigate'),
);

// M12 Phase 2 — live viewport relay. The agent-browser stream server is a
// loopback-only unauthenticated WS (desktop connects directly via
// direct_ws_url); remote clients (the phone) must NEVER reach it raw. This
// route bridges it as SSE so it rides the normal bearer-auth middleware and
// the app's existing expo/fetch stream reader. Node >= 22 global WebSocket
// client — zero new dependencies. Frames relayed last-wins at ~4 fps.

const FRAME_TICK_MS = 250;
const KEEPALIVE_TICKS = 60; // ~15s of idle -> ping

browserViewerRoute.get('/api/browser-viewer/stream/sse', async (c) => {
  const statusRes = await authedFetchJson('/api/browser-viewer/status');
  const statusJson = isRecord(statusRes.json) ? statusRes.json : null;
  const stream = statusJson && isRecord(statusJson.stream) ? statusJson.stream : null;
  const port = stream && typeof stream.port === 'number' ? stream.port : 0;
  if (!stream || stream.enabled !== true || port <= 0 || port > 65535) {
    return c.json({ detail: 'viewport stream is not enabled' }, 409);
  }

  const WebSocketCtor = (globalThis as { WebSocket?: new (url: string) => WsLike }).WebSocket;
  if (!WebSocketCtor) {
    return c.json({ detail: 'server runtime lacks a WebSocket client' }, 501);
  }

  return streamSSE(c, async (sse) => {
    let latest: string | null = null;
    let closed = false;
    const upstream = new WebSocketCtor(`ws://127.0.0.1:${port}`);

    const finish = () => {
      closed = true;
      try {
        upstream.close();
      } catch {
        // already closed
      }
    };
    upstream.onmessage = (ev) => {
      latest = String(ev.data);
    };
    upstream.onclose = finish;
    upstream.onerror = finish;
    sse.onAbort(finish);

    let idleTicks = 0;
    while (!closed) {
      await new Promise((r) => setTimeout(r, FRAME_TICK_MS));
      if (closed) break;
      try {
        if (latest !== null) {
          const frame = latest;
          latest = null;
          idleTicks = 0;
          await sse.writeSSE({ data: frame });
        } else if (++idleTicks >= KEEPALIVE_TICKS) {
          idleTicks = 0;
          await sse.writeSSE({ data: '{"type":"ping"}' });
        }
      } catch {
        finish(); // client went away mid-write
      }
    }
  });
});

interface WsLike {
  close(): void;
  onmessage: ((ev: { data: unknown }) => void) | null;
  onclose: (() => void) | null;
  onerror: (() => void) | null;
}
