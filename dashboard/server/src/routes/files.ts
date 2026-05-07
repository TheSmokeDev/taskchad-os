/**
 * /api/agents/:id/files routes — note that these live under /api/agents
 * but are a logically distinct surface. The agents.ts module already
 * declares the file routes (list, patch, history) — keeping those there
 * preserves declaration order vs the FastAPI server.
 *
 * This file exists for future expansion (e.g. file restore endpoints
 * forwarded from WS4) and to keep the route fan-out symmetric with the
 * Python side.
 */

import { Hono } from 'hono';
import { inboundPersonaId, outboundPersonaId } from '../translate.js';

void inboundPersonaId;
void outboundPersonaId;

export const filesRoute = new Hono();

// Currently empty — file endpoints declared in agents.ts because the
// FastAPI route prefixes overlap (/api/agents/:id/files). When file
// history/restore endpoints land in a future phase, they go here.
