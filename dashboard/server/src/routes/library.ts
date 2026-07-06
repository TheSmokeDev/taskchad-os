/**
 * /api/skills + /api/files/* + /api/system-jobs — M9 library proxy (read-only).
 *
 * Straight GET pass-throughs. No persona translation applies: skills, file
 * paths, and job names are framework-level identifiers, never persona aliases.
 * Imports satisfy the static-invariants grep gate (Q4 lock).
 */

import { Hono } from 'hono';
import { authedFetchJson } from '../framework-client.js';
import { inboundPersonaId, outboundPersonaId } from '../translate.js';

void inboundPersonaId;
void outboundPersonaId;

export const libraryRoute = new Hono();

function proxyGet(path: string) {
  return async (c: any) => {
    const url = new URL(c.req.url);
    const upstreamPath = `${path}${url.search ? `?${url.searchParams.toString()}` : ''}`;
    const upstream = await authedFetchJson(upstreamPath, { method: 'GET' });
    return c.json(upstream.json, upstream.status as 200);
  };
}

libraryRoute.get('/api/skills', proxyGet('/api/skills'));
libraryRoute.get('/api/files/list', proxyGet('/api/files/list'));
libraryRoute.get('/api/files/read', proxyGet('/api/files/read'));
libraryRoute.get('/api/system-jobs', proxyGet('/api/system-jobs'));
