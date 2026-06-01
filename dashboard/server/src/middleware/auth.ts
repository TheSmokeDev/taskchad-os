/**
 * Auth middleware — Bearer header check on every /api/* request EXCEPT
 * /api/health.
 *
 * Query-token fallback:
 *   - Browser EventSource API CANNOT set custom headers, so SSE endpoints
 *     accept `?token=...` query param.
 *   - Cabinet voice document/static GETs also accept `?token=...` because
 *     they are opened as browser documents/resources, not fetch() calls.
 *   - Hono access logs scrub the token from the URL before write.
 *   - `Referrer-Policy: no-referrer` is set on stream/document responses.
 *   - framework-client.ts MUST never construct `?token=` itself.
 *
 * 4-branch boot policy (R4 NM1):
 *   - Resolved at boot (auth-policy.ts) — middleware reads AUTH_POLICY,
 *     never process.env at request time.
 *   - dev-mode-loopback emits a WARN log line on every request.
 */

import type { MiddlewareHandler, Context } from 'hono';
import { getAuthPolicy } from '../auth-policy.js';
import { logger } from '../logger.js';

const HEALTH_PATH = '/api/health';

/**
 * Extract the bearer token from Authorization header OR an approved query token.
 *
 * Query tokens are permitted ONLY for stream endpoints and Cabinet voice
 * document/static GET endpoints. Other paths must use Authorization header.
 */
function extractToken(c: Context, urlPathname: string): string | null {
  const authHeader = c.req.header('authorization') || c.req.header('Authorization');
  if (authHeader && authHeader.toLowerCase().startsWith('bearer ')) {
    return authHeader.slice(7).trim();
  }
  // Query token fallback — only allowed for stream endpoints and Cabinet
  // voice document/static GET endpoints.
  if (isQueryTokenPath(urlPathname, c.req.method)) {
    const queryToken = c.req.query('token');
    if (queryToken) {
      return queryToken;
    }
  }
  return null;
}

function isQueryTokenPath(pathname: string, method: string): boolean {
  // Matches /api/conversation/<persona_id>/stream and /api/cabinet/stream
  // (Phase 5a, action/query-shaped — meetingId is in the query string).
  if (/^\/api\/conversation\/[^/]+\/stream$/.test(pathname)) return true;
  if (pathname === '/api/cabinet/stream') return true;
  if (method.toUpperCase() !== 'GET') return false;
  if (pathname === '/api/cabinet/voice/ui') return true;
  if (pathname === '/api/cabinet/voice/client.bundle.js') return true;
  if (pathname === '/api/cabinet/voice/client.js') return true;
  if (/^\/api\/cabinet\/voice\/avatars\/[^/]+\.png$/.test(pathname)) return true;
  return false;
}

/**
 * Build the auth middleware. Returns a Hono middleware that:
 *  - lets /api/health through unauthenticated.
 *  - in dev-mode-loopback, emits a WARN log line per request and lets it through.
 *  - in token-equal/token-alias, requires Bearer token (or SSE query) matching policy.expectedToken.
 */
export function buildAuthMiddleware(): MiddlewareHandler {
  return async (c, next) => {
    const url = new URL(c.req.url);
    const pathname = url.pathname;

    // /api/health — always unauthenticated.
    if (pathname === HEALTH_PATH) {
      await next();
      return;
    }

    // Only gate /api/* paths. Static assets / other paths pass through.
    if (!pathname.startsWith('/api/')) {
      await next();
      return;
    }

    const policy = getAuthPolicy();
    if (!policy) {
      // Should never happen — boot guards this.
      logger.error({ pathname }, 'auth middleware: no policy configured');
      return c.json({ error: 'server misconfigured: no auth policy' }, 500);
    }

    if (policy.mode === 'dev-mode-loopback') {
      // Loud warning every request.
      logger.warn(
        {
          pathname,
          method: c.req.method,
          remote: c.req.header('host') ?? 'unknown',
        },
        'WARN: dashboard request served without authentication (DASHBOARD_DEV_MODE_NO_AUTH=true; loopback only)',
      );
      await next();
      return;
    }

    // token-equal or token-alias: extract and compare.
    const provided = extractToken(c, pathname);
    if (!provided || provided !== policy.expectedToken) {
      return c.json({ error: 'Unauthorized' }, 401);
    }

    await next();
  };
}
