/**
 * Hono app factory — assembles middleware + routes into a runnable app.
 *
 * Middleware stack ordering (matches ClaudeClaw donor pattern, dashboard-owner
 * approved):
 *   1. log-scrub (records every request with token-redacted URL)
 *   2. response-headers (security headers on every response)
 *   3. CSRF (mutation origin allowlist)
 *   4. auth (Bearer or SSE-query token, exempts /api/health)
 *   5. routes (health, agents, conversation, scheduled, memories,
 *      hive-mind, settings, mission)
 *
 * The factory is split from index.ts so vitest can mount the app in
 * a node test environment without binding a network port.
 */

import { Hono } from 'hono';
import { buildAuthMiddleware } from './middleware/auth.js';
import { buildCsrfMiddleware } from './middleware/csrf.js';
import { buildResponseHeadersMiddleware } from './middleware/response-headers.js';
import { buildLogScrubMiddleware } from './middleware/log-scrub.js';

import { healthRoute } from './routes/health.js';
import { agentsRoute } from './routes/agents.js';
import { conversationRoute } from './routes/conversation.js';
import { scheduledRoute } from './routes/scheduled.js';
import { memoriesRoute } from './routes/memories.js';
import { hiveMindRoute } from './routes/hive-mind.js';
import { settingsRoute } from './routes/settings.js';
import { missionRoute } from './routes/mission.js';

export function buildDashboardApp(): Hono {
  const app = new Hono();

  // Middleware pipeline.
  app.use('*', buildLogScrubMiddleware());
  app.use('*', buildResponseHeadersMiddleware());
  app.use('*', buildCsrfMiddleware());
  app.use('*', buildAuthMiddleware());

  // Route mounts. Each route module owns its own path prefix.
  app.route('/', healthRoute);
  app.route('/', agentsRoute);
  app.route('/', conversationRoute);
  app.route('/', scheduledRoute);
  app.route('/', memoriesRoute);
  app.route('/', hiveMindRoute);
  app.route('/', settingsRoute);
  app.route('/', missionRoute);

  return app;
}
