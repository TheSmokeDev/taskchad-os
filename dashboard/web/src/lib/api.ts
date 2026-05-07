// Token + chatId come from the URL query string (set by the Telegram deep
// link or by a saved bookmark). We persist both to sessionStorage on first
// load so subsequent navigations keep working without rewriting the URL.
// We never use localStorage: dashboardToken is sensitive, and storing it
// across browser sessions would enlarge its blast radius.
//
// CRITICAL — this file MUST NOT import any YAML library. Q5 single-yaml-
// surface lock: <profile>/config.yaml is parsed by Python only. The
// dashboard/web/src/__tests__/anti-patterns.test.tsx test greps this file
// (and the rest of dashboard/web/) for any yaml/js-yaml import — fails
// the build if matched.
//
// CRITICAL — Rule 1: never bind tunable config in default args. There is
// NO `function fetchX(token = process.env.X)` here. `dashboardToken` is
// resolved on module load from the URL/sessionStorage (which is the
// browser equivalent of a config call-site), not bound into function
// signatures.
//
// CRITICAL — Rule 2: no module-level mutable response cache. We DO cache
// the resolved token + chatId at module scope, but those are immutable
// per page-load (they reflect operator config captured at app-mount, not
// derived state from API responses). This is the same kind of boot-
// snapshot pattern Hono's auth-policy.ts uses, documented as
// intentionally NOT a Rule 2 violation.

const url = typeof window !== 'undefined' ? new URL(window.location.href) : null;

let cachedToken = url?.searchParams.get('token') || '';
if (cachedToken) {
  try { sessionStorage.setItem('homie.token', cachedToken); } catch {}
} else {
  try { cachedToken = sessionStorage.getItem('homie.token') || ''; } catch {}
}

let cachedChatId = url?.searchParams.get('chatId') || '';
if (cachedChatId) {
  try { sessionStorage.setItem('homie.chatId', cachedChatId); } catch {}
} else {
  try { cachedChatId = sessionStorage.getItem('homie.chatId') || ''; } catch {}
}

export const dashboardToken = cachedToken;
export const chatId = cachedChatId;

function bearerHeaders(extra?: Record<string, string>): Record<string, string> {
  const h: Record<string, string> = { ...(extra ?? {}) };
  if (dashboardToken) {
    h['Authorization'] = `Bearer ${dashboardToken}`;
  }
  return h;
}

export class ApiError extends Error {
  constructor(public status: number, public body: unknown, message: string) {
    super(message);
  }
}

export async function apiGet<T = unknown>(path: string): Promise<T> {
  const res = await fetch(path, { method: 'GET', headers: bearerHeaders() });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new ApiError(res.status, body, `GET ${path} failed: ${res.status}`);
  }
  return res.json();
}

export async function apiPost<T = unknown>(path: string, body?: unknown): Promise<T> {
  const headers = bearerHeaders(body ? { 'content-type': 'application/json' } : undefined);
  const res = await fetch(path, {
    method: 'POST',
    headers,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    const errBody = await res.json().catch(() => ({}));
    throw new ApiError(res.status, errBody, `POST ${path} failed: ${res.status}`);
  }
  return res.json();
}

export async function apiPatch<T = unknown>(path: string, body: unknown): Promise<T> {
  const res = await fetch(path, {
    method: 'PATCH',
    headers: bearerHeaders({ 'content-type': 'application/json' }),
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const errBody = await res.json().catch(() => ({}));
    throw new ApiError(res.status, errBody, `PATCH ${path} failed: ${res.status}`);
  }
  return res.json();
}

export async function apiPut<T = unknown>(path: string, body: unknown): Promise<T> {
  const res = await fetch(path, {
    method: 'PUT',
    headers: bearerHeaders({ 'content-type': 'application/json' }),
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const errBody = await res.json().catch(() => ({}));
    throw new ApiError(res.status, errBody, `PUT ${path} failed: ${res.status}`);
  }
  return res.json();
}

/** Multipart PUT — used for avatar uploads. Lets the browser set the
 *  Content-Type with the multipart boundary; we only inject Bearer. */
export async function apiPutForm<T = unknown>(path: string, form: FormData): Promise<T> {
  const res = await fetch(path, {
    method: 'PUT',
    headers: bearerHeaders(),
    body: form,
  });
  if (!res.ok) {
    const errBody = await res.json().catch(() => ({}));
    throw new ApiError(res.status, errBody, `PUT ${path} failed: ${res.status}`);
  }
  return res.json();
}

export async function apiDelete<T = unknown>(path: string): Promise<T> {
  const res = await fetch(path, { method: 'DELETE', headers: bearerHeaders() });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new ApiError(res.status, body, `DELETE ${path} failed: ${res.status}`);
  }
  return res.json();
}

/** Build a URL for SSE endpoints with the token embedded as ?token=...
 *  Browser EventSource cannot set Authorization headers, so SSE is the
 *  ONE place tokens travel via URL. The Hono auth middleware accepts
 *  this query token only on /api/conversation/<id>/stream and scrubs it
 *  from access logs. NEVER use this helper for non-SSE endpoints. */
export function tokenizedSseUrl(path: string): string {
  if (!dashboardToken) return path;
  const sep = path.includes('?') ? '&' : '?';
  return `${path}${sep}token=${encodeURIComponent(dashboardToken)}`;
}
