/**
 * Auth middleware — Bearer header check on every /api/* request EXCEPT
 * /api/health.
 *
 * SSE token via query (owner Decision 4 / R1 M4):
 *   - Browser EventSource API CANNOT set custom headers, so SSE endpoints
 *     accept `?token=...` query param BUT only:
 *       (a) browser → Hono — never forwarded to Python.
 *       (b) Hono access logs scrub the token from the URL before write.
 *       (c) `Referrer-Policy: no-referrer` set on SSE responses.
 *     framework-client.ts MUST never include `?token=` in upstream URL.
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
 * Extract the bearer token from Authorization header OR the SSE query token.
 *
 * The SSE query token is permitted ONLY for SSE endpoints (path matches
 * /api/conversation/<id>/stream). Other paths must use Authorization header.
 */
function extractToken(c: Context, urlPathname: string): string | null {
  const authHeader = c.req.header('authorization') || c.req.header('Authorization');
  if (authHeader && authHeader.toLowerCase().startsWith('bearer ')) {
    return authHeader.slice(7).trim();
  }
  // SSE query token fallback — only allowed for stream endpoints.
  if (isSseStreamPath(urlPathname)) {
    const queryToken = c.req.query('token');
    if (queryToken) {
      return queryToken;
    }
  }
  return null;
}

function isSseStreamPath(pathname: string): boolean {
  // Matches /api/conversation/<persona_id>/stream and similar.
  return /^\/api\/conversation\/[^/]+\/stream$/.test(pathname);
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
