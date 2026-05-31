/**
 * mission.test.ts — orchestration passthrough contract.
 */

import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { ROUTE_MANIFEST } from '../routes.js';

const MISSION_ROUTE = join(__dirname, '..', 'routes', 'mission.ts');

describe('mission orchestration route', () => {
  it('registers the Convoy/Team operator APIs used by the dashboard pages', () => {
    expect(ROUTE_MANIFEST).toContain('/api/convoy');
    expect(ROUTE_MANIFEST).toContain('/api/convoy/:id');
    expect(ROUTE_MANIFEST).toContain('/api/convoy/:id/subtask/:sid/dispatch');
    expect(ROUTE_MANIFEST).toContain('/api/mailbox/send');
    expect(ROUTE_MANIFEST).toContain('/api/mailbox/claim/:agent');
    expect(ROUTE_MANIFEST).toContain('/api/mailbox/convoy/:id');
    expect(ROUTE_MANIFEST).toContain('/api/team');
    expect(ROUTE_MANIFEST).toContain('/api/team/taskchad-drill');
    expect(ROUTE_MANIFEST).toContain('/api/team/room/run');
    expect(ROUTE_MANIFEST).toContain('/api/team/:id');
    expect(ROUTE_MANIFEST).toContain('/api/team/:id/members');
    expect(ROUTE_MANIFEST).toContain('/api/team/:id/shutdown');
    expect(ROUTE_MANIFEST).toContain('/api/team/:id/loop-step');
    expect(ROUTE_MANIFEST).toContain('/api/team/:id/tick');
    expect(ROUTE_MANIFEST).toContain('/api/team/:id/executor-step');
  });

  it('keeps Convoy and Team as thin pass-throughs to Python orchestration', () => {
    const src = readFileSync(MISSION_ROUTE, 'utf-8');
    expect(src).toContain("'/api/convoy'");
    expect(src).toContain("'/api/mailbox'");
    expect(src).toContain("'/api/team'");
    expect(src).toContain('authedFetch(upstreamPath');
    expect(src).not.toMatch(/better-sqlite3|\bnew\s+Database\(|sqlite3/);
    expect(src).not.toMatch(/readFileSync|config\.yaml|TheHomie\/Memory/);
  });
});
