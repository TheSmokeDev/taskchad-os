/**
 * CSRF / origin enforcement middleware.
 *
 * Pattern ported from ClaudeClaw donor (dashboard.ts:323-373):
 *   - GET/HEAD/OPTIONS → pass through.
 *   - Mutations (POST/PATCH/PUT/DELETE) without Origin → allow (CLI tools, fetch from same page).
 *   - Mutations with Origin → check against allowlist:
 *       * localhost / 127.0.0.1 / ::1 / 0.0.0.0
 *       * DASHBOARD_URL host (if configured — for tunneled access)
 *
 * Anti-pattern compliance:
 *   - DASHBOARD_URL is read on every request (Rule 2). Operators may
 *     configure it post-boot; CSRF allowlist must reflect current value.
 */

import type { MiddlewareHandler } from 'hono';
import { logger } from '../logger.js';

const SAFE_HOSTS = new Set(['localhost', '127.0.0.1', '[::1]', '::1', '0.0.0.0']);

function getAllowedOriginHost(): string {
  const raw = (process.env.DASHBOARD_URL ?? '').trim();
  if (!raw) return '';
  try {
    return new URL(raw).hostname;
  } catch {
    return '';
  }
}

export function buildCsrfMiddleware(): MiddlewareHandler {
  return async (c, next) => {
    const method = c.req.method;
    if (method === 'GET' || method === 'HEAD' || method === 'OPTIONS') {
      await next();
      return;
    }

    const origin = c.req.header('origin');
    if (origin) {
      let host = '';
      try {
        host = new URL(origin).hostname;
      } catch {
        // malformed Origin header — treat as cross-origin and reject below.
      }
      const allowedHost = getAllowedOriginHost();
      const allowed = SAFE_HOSTS.has(host) || (!!allowedHost && host === allowedHost);
      if (!allowed) {
        logger.warn(
          { origin, method, pathname: new URL(c.req.url).pathname },
          'CSRF: rejected cross-origin request',
        );
        return c.json({ error: 'cross-origin request rejected' }, 403);
      }
    }
    await next();
  };
}
