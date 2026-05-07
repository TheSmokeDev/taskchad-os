/**
 * Pino logger singleton — used by middleware + route handlers.
 *
 * Level resolved on every property access via getter is overkill; pino
 * captures level at construction. Operators set DASHBOARD_LOG_LEVEL.
 */

import pino from 'pino';

function resolveLevel(): string {
  const raw = (process.env.DASHBOARD_LOG_LEVEL ?? '').trim().toLowerCase();
  if (raw === 'debug' || raw === 'info' || raw === 'warn' || raw === 'error' || raw === 'fatal' || raw === 'trace') {
    return raw;
  }
  return 'info';
}

export const logger = pino({
  level: resolveLevel(),
  base: {
    service: 'dashboard-server',
  },
});
