/**
 * Response-header middleware — security headers on every response.
 *
 * Sets:
 *   - X-Content-Type-Options: nosniff
 *   - X-Frame-Options: DENY
 *   - Referrer-Policy: no-referrer (defense-in-depth — SSE endpoints
 *     also set this explicitly per owner Decision 4 / R1 M4)
 *   - Cache-Control: no-store on /api/* (operator data, never cache)
 */

import type { MiddlewareHandler } from 'hono';

export function buildResponseHeadersMiddleware(): MiddlewareHandler {
  return async (c, next) => {
    await next();
    c.res.headers.set('X-Content-Type-Options', 'nosniff');
    c.res.headers.set('X-Frame-Options', 'DENY');
    c.res.headers.set('Referrer-Policy', 'no-referrer');
    const pathname = new URL(c.req.url).pathname;
    if (pathname.startsWith('/api/')) {
      // SSE handlers may override Cache-Control to no-cache; that's fine.
      if (!c.res.headers.has('Cache-Control')) {
        c.res.headers.set('Cache-Control', 'no-store');
      }
    }
  };
}
