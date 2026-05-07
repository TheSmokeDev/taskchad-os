/**
 * Framework HTTP client — the ONLY surface that talks to the Python
 * framework on port 4322 from inside the Hono dashboard server.
 *
 * Slice ownership:
 *   - Hono routes import this module to call framework endpoints.
 *   - Hono routes MUST NOT use bare fetch() against the Python framework.
 *   - This module never reads `<profile>/config.yaml` — Q5 single-yaml-surface lock.
 *   - This module never opens SQLite — thin-proxy boundary.
 *
 * Anti-pattern compliance (TS Rule 1 + Rule 2):
 *   - No tunable config bound in default args. `token?: string` resolved
 *     in body via `token ?? AUTH_POLICY.expectedToken ?? undefined`.
 *   - No module-level mutable cache except the deliberately-snapshotted
 *     AUTH_POLICY (in auth-policy.ts).
 *
 * Auth forwarding contract:
 *   - Browser→Hono uses Bearer header (or query token for SSE only).
 *   - Hono→Python ALWAYS uses Bearer header — never `?token=` in URL.
 *     This is enforced by the sse-token-hardening grep test.
 */

import { getAuthPolicy } from './auth-policy.js';

/** Resolve the framework base URL — read on every call (Rule 2). */
function getFrameworkUrl(): string {
  return process.env.FRAMEWORK_API_URL ?? 'http://127.0.0.1:4322';
}

/** Resolve the bearer token — boot-snapshot or explicit override. */
function resolveBearerToken(token?: string): string | null {
  if (token !== undefined) {
    return token;
  }
  const policy = getAuthPolicy();
  return policy?.expectedToken ?? null;
}

export interface FetchOptions {
  method?: string;
  headers?: Record<string, string>;
  body?: string | FormData | Uint8Array;
  /** Caller-provided token override; otherwise boot-snapshot is used. */
  token?: string;
  /** Last-Event-ID header for SSE resume. */
  lastEventId?: string | null;
}

export interface FetchResult {
  status: number;
  ok: boolean;
  headers: Headers;
  body: string;
  json: () => unknown;
}

/**
 * Authenticated fetch helper — adds Bearer header, never `?token=`.
 *
 * Args:
 *   - path: framework path starting with `/` (e.g. `/api/agents`).
 *   - options: standard fetch options + token override.
 *
 * Throws on network error; HTTP errors are returned in the result.
 */
export async function authedFetch(path: string, options: FetchOptions = {}): Promise<FetchResult> {
  const url = `${getFrameworkUrl()}${path}`;
  const headers: Record<string, string> = {
    ...(options.headers ?? {}),
  };

  // Resolve token in body (Rule 1).
  const bearer = resolveBearerToken(options.token);
  if (bearer) {
    headers['Authorization'] = `Bearer ${bearer}`;
  }
  if (options.lastEventId !== undefined && options.lastEventId !== null) {
    headers['Last-Event-ID'] = options.lastEventId;
  }

  const init: RequestInit = {
    method: options.method ?? 'GET',
    headers,
  };
  if (options.body !== undefined) {
    init.body = options.body as BodyInit;
  }

  const resp = await fetch(url, init);
  const text = await resp.text();
  return {
    status: resp.status,
    ok: resp.ok,
    headers: resp.headers,
    body: text,
    json: () => {
      try {
        return JSON.parse(text);
      } catch {
        return null;
      }
    },
  };
}

/**
 * SSE proxy fetch — returns the raw Response object so the route handler
 * can stream byte-for-byte. No Bearer-vs-query translation here; routes
 * MUST translate the browser query token into a Bearer header before
 * calling this.
 */
export async function authedFetchStream(
  path: string,
  options: FetchOptions = {},
): Promise<Response> {
  const url = `${getFrameworkUrl()}${path}`;
  const headers: Record<string, string> = {
    ...(options.headers ?? {}),
  };

  const bearer = resolveBearerToken(options.token);
  if (bearer) {
    headers['Authorization'] = `Bearer ${bearer}`;
  }
  if (options.lastEventId !== undefined && options.lastEventId !== null) {
    headers['Last-Event-ID'] = options.lastEventId;
  }

  return fetch(url, {
    method: options.method ?? 'GET',
    headers,
  });
}

/**
 * JSON fetch helper — wraps authedFetch and parses response.
 */
export async function authedFetchJson(
  path: string,
  options: FetchOptions = {},
): Promise<{ status: number; ok: boolean; json: unknown; headers: Headers }> {
  const result = await authedFetch(path, {
    ...options,
    headers: {
      ...(options.headers ?? {}),
      Accept: 'application/json',
    },
  });
  return {
    status: result.status,
    ok: result.ok,
    json: result.json(),
    headers: result.headers,
  };
}

/**
 * Multipart body forwarder — used by avatar PUT.
 */
export async function authedFetchMultipart(
  path: string,
  formData: FormData,
  options: Omit<FetchOptions, 'body'> = {},
): Promise<FetchResult> {
  const url = `${getFrameworkUrl()}${path}`;
  const headers: Record<string, string> = {
    ...(options.headers ?? {}),
  };

  const bearer = resolveBearerToken(options.token);
  if (bearer) {
    headers['Authorization'] = `Bearer ${bearer}`;
  }
  // Do NOT set Content-Type — fetch sets it (with boundary) automatically for FormData.

  const resp = await fetch(url, {
    method: options.method ?? 'PUT',
    headers,
    body: formData,
  });
  const text = await resp.text();
  return {
    status: resp.status,
    ok: resp.ok,
    headers: resp.headers,
    body: text,
    json: () => {
      try {
        return JSON.parse(text);
      } catch {
        return null;
      }
    },
  };
}
