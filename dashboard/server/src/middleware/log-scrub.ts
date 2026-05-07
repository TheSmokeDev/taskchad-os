/**
 * Log-scrub middleware (owner Decision 4 / R1 M4).
 *
 * Hono access logs MUST scrub `?token=...` query strings before write.
 * This is the access-log writer; route handlers should use this helper
 * to log requests instead of pino directly when the URL is involved.
 *
 * The scrub is a regex on the URL — replaces `token=<value>` with
 * `token=<redacted>` for logging only. Original c.req.url is unchanged.
 */

import type { MiddlewareHandler } from 'hono';
import { logger } from '../logger.js';

const TOKEN_QUERY_RE = /([?&]token=)[^&]*/gi;

/**
 * Scrub the token query parameter from a URL string for logging.
 *
 * Public so tests can verify the function directly.
 */
export function scrubTokenFromUrl(url: string): string {
  return url.replace(TOKEN_QUERY_RE, '$1<redacted>');
}

export function buildLogScrubMiddleware(): MiddlewareHandler {
  return async (c, next) => {
    const start = Date.now();
    await next();
    const duration = Date.now() - start;
    const scrubbedUrl = scrubTokenFromUrl(c.req.url);
    logger.info(
      {
        method: c.req.method,
        url: scrubbedUrl,
        status: c.res.status,
        duration_ms: duration,
      },
      'request',
    );
  };
}
