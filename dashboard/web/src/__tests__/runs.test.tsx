/**
 * runs.test.tsx — the standalone Runs page (voice-deployed worker visibility).
 *
 * Coverage:
 *   - Static contract: page registered in App.tsx router + lib/routes.ts nav,
 *     polls the history-merged runs endpoint, and reuses RunSidebar.
 *   - Renders run rows (kind, label, status) from the endpoint, including the
 *     `lost` status for runs whose API process died.
 *   - Empty state renders when no runs exist.
 */

import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/preact';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { Runs } from '@/pages/Runs';

const WEB_SRC = join(__dirname, '..');

function stubFetch(runs: unknown[]) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const path = String(input);
    if (path.startsWith('/api/talk/runs')) {
      return new Response(JSON.stringify({ ok: true, runs }), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      });
    }
    return new Response('{}', {
      status: 200,
      headers: { 'content-type': 'application/json' },
    });
  });
  globalThis.fetch = fetchMock as unknown as typeof fetch;
  return fetchMock;
}

describe('runs page — static contract', () => {
  it('is registered in the router and nav registry', () => {
    const app = readFileSync(join(WEB_SRC, 'App.tsx'), 'utf-8');
    expect(app).toContain("import { Runs } from '@/pages/Runs'");
    expect(app).toContain('<Route path="/runs">');
    const routes = readFileSync(join(WEB_SRC, 'lib', 'routes.ts'), 'utf-8');
    expect(routes).toContain("path: '/runs'");
  });

  it('polls the history-merged runs endpoint and reuses RunSidebar', () => {
    const src = readFileSync(join(WEB_SRC, 'pages', 'Runs.tsx'), 'utf-8');
    expect(src).toContain('/api/talk/runs?limit=50&history=true');
    expect(src).toContain('<RunSidebar />');
  });
});

describe('runs page — rendering', () => {
  it('renders run rows including lost runs from dead API processes', async () => {
    stubFetch([
      { runId: 12, kind: 'agent', label: 'audit the site', status: 'running', output: '', ts: 1, updated: 1 },
      { runId: 11, kind: 'skill', label: 'vault ops', status: 'done', output: 'all good', ts: 1, updated: 1, fromHistory: true },
      { runId: 9, kind: 'archon', label: 'clutch build', status: 'lost', output: '', ts: 1, updated: 1, fromHistory: true },
    ]);
    render(<Runs />);

    await screen.findByText('audit the site');
    expect(screen.getByText('vault ops')).toBeTruthy();
    expect(screen.getByText('clutch build')).toBeTruthy();
    expect(screen.getByText('lost')).toBeTruthy();
    expect(screen.getByText(/1 running \/ 3 recent/)).toBeTruthy();
  });

  it('renders the empty state when no runs exist', async () => {
    stubFetch([]);
    render(<Runs />);

    await screen.findByText('No runs yet');
  });
});
