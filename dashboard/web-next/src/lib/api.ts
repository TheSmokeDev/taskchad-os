// This is the React equivalent of dashboard/web/src/lib/api.ts. The token is
// a page-load snapshot, never a response cache. Python remains the only YAML
// and configuration authority.

const pageUrl = typeof window !== 'undefined' ? new URL(window.location.href) : null;

let cachedToken = pageUrl?.searchParams.get('token') || '';
if (cachedToken) {
  try {
    sessionStorage.setItem('homie.token', cachedToken);
  } catch {
    // Session storage can be disabled. Keep the in-memory page-load token.
  }
} else {
  try {
    cachedToken = sessionStorage.getItem('homie.token') || '';
  } catch {
    // An empty token is valid for the loopback dev-mode stack.
  }
}

export const dashboardToken = cachedToken;

function bearerHeaders(extra?: Record<string, string>): Record<string, string> {
  const headers = { ...(extra ?? {}) };
  if (dashboardToken) headers.Authorization = `Bearer ${dashboardToken}`;
  return headers;
}

export class ApiError extends Error {
  constructor(
    public status: number,
    public body: unknown,
    message: string,
  ) {
    super(message);
  }
}

async function errorBody(response: Response): Promise<unknown> {
  return response.json().catch(() => ({}));
}

export async function apiGet<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(path, {
    method: 'GET',
    headers: bearerHeaders(),
    signal,
  });
  if (!response.ok) {
    throw new ApiError(response.status, await errorBody(response), `GET ${path} failed`);
  }
  return response.json() as Promise<T>;
}

export async function apiGetBlob(path: string, signal?: AbortSignal): Promise<Blob> {
  const response = await fetch(path, {
    method: 'GET',
    headers: bearerHeaders(),
    signal,
  });
  if (!response.ok) {
    throw new ApiError(response.status, await errorBody(response), `GET ${path} failed`);
  }
  return response.blob();
}

export async function apiPost<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(path, {
    method: 'POST',
    headers: bearerHeaders({ 'content-type': 'application/json' }),
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    throw new ApiError(response.status, await errorBody(response), `POST ${path} failed`);
  }
  return response.json() as Promise<T>;
}

/**
 * EventSource cannot set Authorization. This helper is restricted to the
 * conversation SSE route whose Hono middleware accepts and scrubs the token.
 */
export function tokenizedConversationStream(path: string): string {
  if (!dashboardToken) return path;
  const separator = path.includes('?') ? '&' : '?';
  return `${path}${separator}token=${encodeURIComponent(dashboardToken)}`;
}
