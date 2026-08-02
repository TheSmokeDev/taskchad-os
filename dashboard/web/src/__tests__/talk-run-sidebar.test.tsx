/**
 * talk-run-sidebar.test.tsx — the Talk page's live run sidebar (#257 / F9).
 *
 * Behavior under test, one case per distinct path:
 *   - a REST snapshot renders workflow name, status, current node and tool
 *     durations (the four things the ticket asks a card to show);
 *   - the empty state, and each honest degraded state — ledger missing,
 *     REST failing, kill-switch 503 — none of which may leave a spinner up;
 *   - a kill-switch 503 must not open a stream;
 *   - live SSE frames mutate a rendered card (approval highlight) and the
 *     phase pill tracks socket health (live / polling);
 *   - the Talk page layout contract: two columns at `lg`, one below it.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/preact';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { RunSidebar } from '@/components/RunSidebar';
import { Talk } from '@/pages/Talk';

const WEB_SRC = join(__dirname, '..');

type EventHandler = ((event?: unknown) => void | Promise<void>) | null;

class FakeEventSource {
  static instances: FakeEventSource[] = [];

  onopen: EventHandler = null;
  onerror: EventHandler = null;
  readonly listeners = new Map<string, (event: MessageEvent) => void>();
  readonly url: string;
  closed = false;

  constructor(url: string) {
    this.url = url;
    FakeEventSource.instances.push(this);
  }

  addEventListener(name: string, handler: (event: MessageEvent) => void): void {
    this.listeners.set(name, handler);
  }

  close(): void {
    this.closed = true;
  }

  emit(payload: unknown): void {
    this.listeners.get('message')?.({ data: JSON.stringify(payload) } as MessageEvent);
  }
}

/** Verbatim `GET /api/archon/events` body shape (dashboard_api.py:7489-7501). */
function snapshotBody(overrides: Record<string, unknown> = {}) {
  return {
    ok: true,
    status: 'ok',
    runStatus: 'ok',
    runId: null,
    conversationId: null,
    events: [],
    runs: [],
    latestSeq: 12,
    poller: { running: true, status: 'ok' },
    ...overrides,
  };
}

/** Route the archon REST call; everything else answers an empty 200. */
function stubArchonFetch(responder: () => Response) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    if (String(input).startsWith('/api/archon/events')) return responder();
    return new Response('{}', { status: 200, headers: { 'content-type': 'application/json' } });
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

describe('RunSidebar', () => {
  beforeEach(() => {
    FakeEventSource.instances = [];
    vi.stubGlobal('EventSource', FakeEventSource);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('renders workflow name, status, current node and tool durations', async () => {
    stubArchonFetch(() =>
      jsonResponse(
        snapshotBody({
          runs: [
            {
              runId: 'run-a',
              conversationId: 'conv-a',
              workflowName: 'archon-piv-loop',
              status: 'running',
              startedAt: '2026-07-27 19:59:00',
              completedAt: null,
              lastActivityAt: '2026-07-27 20:00:00',
              workingPath: null,
            },
          ],
          events: [
            {
              id: 'e1',
              runId: 'run-a',
              type: 'node_started',
              stepIndex: 2,
              stepName: 'implement',
              createdAt: '2026-07-27 20:00:01',
              data: {},
            },
            {
              id: 'e2',
              runId: 'run-a',
              type: 'tool_called',
              stepIndex: 2,
              stepName: 'implement',
              createdAt: '2026-07-27 20:00:02',
              data: { tool_name: 'Edit', tool_call_id: 'c1' },
            },
            {
              id: 'e3',
              runId: 'run-a',
              type: 'tool_completed',
              stepIndex: 2,
              stepName: 'implement',
              createdAt: '2026-07-27 20:00:03',
              data: { tool_name: 'Edit', tool_call_id: 'c1', duration_ms: 1500 },
            },
          ],
        }),
      ),
    );

    render(<RunSidebar />);

    await screen.findByText('archon-piv-loop');
    expect(screen.getByText('running')).toBeInTheDocument();
    expect(screen.getByText(/Node: implement/)).toBeInTheDocument();
    expect(screen.getByText('Edit')).toBeInTheDocument();
    expect(screen.getByText('1.5s')).toBeInTheDocument();
  });

  it('shows an empty state instead of a spinner when there are no runs', async () => {
    stubArchonFetch(() => jsonResponse(snapshotBody()));
    render(<RunSidebar />);
    await screen.findByText(/No Archon runs yet/);
    expect(screen.queryByText(/Loading Archon telemetry/)).toBeNull();
  });

  it('reports a missing ledger honestly and stops loading', async () => {
    stubArchonFetch(() => jsonResponse(snapshotBody({ status: 'db_missing' })));
    render(<RunSidebar />);
    await screen.findByText(/isn't on this machine yet/);
    expect(screen.getByTestId('archon-phase')).toHaveTextContent('unavailable');
    expect(screen.queryByText(/Loading Archon telemetry/)).toBeNull();
  });

  it('reports a failing REST snapshot as unavailable, never a forever spinner', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => { throw new TypeError('Failed to fetch'); }));
    render(<RunSidebar />);
    await screen.findByText(/Local stack is offline/);
    expect(screen.getByTestId('archon-phase')).toHaveTextContent('unavailable');
    expect(screen.queryByText(/Loading Archon telemetry/)).toBeNull();
  });

  it('surfaces the kill-switch 503 and never opens a stream', async () => {
    stubArchonFetch(() =>
      jsonResponse({ detail: { error: 'archon event ingest is disabled by operator' } }, 503),
    );
    render(<RunSidebar />);
    await screen.findByText('archon event ingest is disabled by operator');
    expect(screen.getByTestId('archon-phase')).toHaveTextContent('disabled');
    expect(FakeEventSource.instances).toHaveLength(0);
  });

  it('resumes the stream at the snapshot seq and goes live on open', async () => {
    stubArchonFetch(() => jsonResponse(snapshotBody({ latestSeq: 41 })));
    render(<RunSidebar />);
    await screen.findByText(/No Archon runs yet/);

    await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1));
    expect(FakeEventSource.instances[0].url).toContain('sinceSeq=41');

    FakeEventSource.instances[0].onopen?.();
    await waitFor(() => expect(screen.getByTestId('archon-phase')).toHaveTextContent('live'));
  });

  it('applies a live approval frame to a rendered card and highlights it', async () => {
    stubArchonFetch(() =>
      jsonResponse(
        snapshotBody({
          runs: [
            {
              runId: 'run-b',
              conversationId: 'conv-b',
              workflowName: 'archon-ralph-dag',
              status: 'running',
              startedAt: '2026-07-27 19:00:00',
              completedAt: null,
              lastActivityAt: '2026-07-27 19:00:00',
              workingPath: null,
            },
          ],
        }),
      ),
    );
    render(<RunSidebar />);
    await screen.findByText('archon-ralph-dag');
    await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1));

    FakeEventSource.instances[0].emit({
      seq: 42,
      event: {
        id: 'e-approval',
        runId: 'run-b',
        type: 'approval_requested',
        stepIndex: null,
        stepName: null,
        createdAt: '2026-07-27 20:10:00',
        data: { message: 'Approve the production deploy?' },
      },
    });

    await screen.findByText(/Approve the production deploy\?/);
    expect(screen.getByTestId('archon-run-run-b').className).toContain('border-amber-500');
  });

  it('falls back to polling when the socket drops', async () => {
    stubArchonFetch(() => jsonResponse(snapshotBody()));
    render(<RunSidebar />);
    await screen.findByText(/No Archon runs yet/);
    await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1));

    FakeEventSource.instances[0].onopen?.();
    await waitFor(() => expect(screen.getByTestId('archon-phase')).toHaveTextContent('live'));

    await FakeEventSource.instances[0].onerror?.({ type: 'error' });
    await waitFor(() => expect(screen.getByTestId('archon-phase')).toHaveTextContent('polling'));
  });

  it('closes the stream on unmount', async () => {
    stubArchonFetch(() => jsonResponse(snapshotBody()));
    const view = render(<RunSidebar />);
    await screen.findByText(/No Archon runs yet/);
    await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1));
    view.unmount();
    expect(FakeEventSource.instances[0].closed).toBe(true);
  });
});

describe('Talk page layout — F9 two-column conversion', () => {
  beforeEach(() => {
    FakeEventSource.instances = [];
    vi.stubGlobal('EventSource', FakeEventSource);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('mounts the run sidebar inside the Talk page', async () => {
    stubArchonFetch(() => jsonResponse(snapshotBody()));
    render(<Talk />);
    await screen.findByTestId('archon-run-sidebar');
  });

  it('is a single column below lg and 1fr+360px at lg', () => {
    const src = readFileSync(join(WEB_SRC, 'pages', 'Talk.tsx'), 'utf-8');
    const wrapper = /<div class="([^"]*lg:grid-cols-\[1fr_360px\][^"]*)"/.exec(src);
    expect(wrapper).not.toBeNull();
    const classes = wrapper![1].split(/\s+/);
    // The split is `lg:`-prefixed ONLY — an unprefixed grid-cols- token would
    // mean two columns on a phone, which is the horizontal-scroll failure.
    expect(classes).toContain('lg:grid-cols-[1fr_360px]');
    expect(classes.filter((c) => c.startsWith('grid-cols-'))).toEqual([]);
    // The conversation column keeps its original reading measure.
    expect(classes).toContain('max-w-3xl');
    expect(classes).toContain('lg:max-w-[1144px]');
    expect(src).toContain('<RunSidebar />');
  });

  it('both grid cells carry min-w-0 so nothing forces a horizontal scroll', () => {
    const talkSrc = readFileSync(join(WEB_SRC, 'pages', 'Talk.tsx'), 'utf-8');
    expect(talkSrc).toContain('<div class="grid gap-4 min-w-0">');
    const sidebarSrc = readFileSync(join(WEB_SRC, 'components', 'RunSidebar.tsx'), 'utf-8');
    expect(sidebarSrc).toContain('<aside class="min-w-0"');
  });
});
