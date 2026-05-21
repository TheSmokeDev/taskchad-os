import { describe, test, expect, beforeEach, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/preact';
import { HiveMind } from '@/pages/HiveMind';

function brainPayload({ hasMore = false, offset = 0 }: { hasMore?: boolean; offset?: number } = {}) {
  const secondPage = offset > 0;
  return {
    nodes: [
      ...(secondPage ? [] : [{
        id: 'chunk:1',
        label: 'Core memory',
        kind: 'chunk',
        scope_type: 'global',
        scope_id: 'main',
        visibility: 'shared',
        source_path: 'MEMORY.md',
        section_title: 'Core',
        text: 'The Homie remembers the global brain.',
        tags: ['vault-chunk', 'global'],
        created_at: 1760000000,
      }]),
      {
        id: 'note:MEMORY.md',
        label: 'MEMORY',
        kind: 'note',
        scope_type: 'global',
        scope_id: 'main',
        visibility: 'shared',
        source_path: 'MEMORY.md',
        tags: ['vault-note'],
      },
      ...(secondPage ? [{
        id: 'chunk:2',
        label: 'Follow-up memory',
        kind: 'chunk',
        scope_type: 'global',
        scope_id: 'main',
        visibility: 'shared',
        source_path: 'MEMORY.md',
        section_title: 'Follow-up',
        text: 'The loaded audit page includes a second memory.',
        tags: ['vault-chunk', 'global'],
        created_at: 1760000100,
      }] : []),
    ],
    edges: [
      ...(secondPage
        ? [{ id: 'source:chunk:2->note:MEMORY.md', source: 'chunk:2', target: 'note:MEMORY.md', kind: 'source' }]
        : [{ id: 'source:chunk:1->note:MEMORY.md', source: 'chunk:1', target: 'note:MEMORY.md', kind: 'source' }]),
    ],
    activity: [
      {
        id: 'chat-1',
        eventId: 1,
        persona_id: 'main',
        personaId: 'main',
        type: 'chat_message',
        role: 'assistant',
        timestamp: Date.now() / 1000,
        details: 'Main recent hive activity',
        provider: 'claude',
        model: 'opus',
      },
      {
        id: 'chat-2',
        eventId: 2,
        persona_id: 'research',
        personaId: 'research',
        type: 'chat_message',
        role: 'user',
        timestamp: Date.now() / 1000,
        details: 'Research handoff activity',
      },
    ],
    layers: { memory: true, activity: true, scopes: ['global/main'] },
    stats: {
      total_nodes: secondPage ? 2 : 2,
      total_edges: 1,
      activity_count: 2,
      total_chunks: hasMore ? 2 : 1,
      scopes: [{ scope_type: 'global', scope_id: 'main', count: 2 }],
      memory: {
        total_chunks: hasMore ? 2 : 1,
        matching_chunks: hasMore ? 2 : 1,
        returned_chunks: 1,
        page: {
          limit: 300,
          offset,
          returned_chunks: 1,
          matching_chunks: hasMore ? 2 : 1,
          has_more: hasMore && !secondPage,
        },
      },
    },
  };
}

function mockBrainFetch({ hasMore = false }: { hasMore?: boolean } = {}) {
  const calls: string[] = [];
  globalThis.fetch = vi.fn(async (url: string) => {
    calls.push(url);
    if (url.includes('/api/agents')) {
      return new Response(JSON.stringify({
        agents: [
          { id: 'main' },
          { id: 'research' },
        ],
      }), { status: 200, headers: { 'content-type': 'application/json' } });
    }
    if (url.includes('/api/brain/graph')) {
      const offset = Number(new URL(url, 'http://localhost').searchParams.get('offset') ?? '0');
      return new Response(JSON.stringify(brainPayload({ hasMore, offset })), { status: 200, headers: { 'content-type': 'application/json' } });
    }
    return new Response(JSON.stringify({}), { status: 200, headers: { 'content-type': 'application/json' } });
  }) as any;
  return calls;
}

describe('HiveMind page', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    localStorage.clear();
  });

  test('renders the activity table from the shared /api/brain/graph contract', async () => {
    localStorage.setItem('homie.hive.view', 'activity');
    const calls = mockBrainFetch();

    render(<HiveMind />);

    await waitFor(() => expect(screen.getByText('Main recent hive activity')).toBeInTheDocument());
    expect(screen.getByText('Research handoff activity')).toBeInTheDocument();
    expect(screen.getByText('chat_message:assistant')).toBeInTheDocument();
    expect(calls.some((url) => url.includes('/api/brain/graph') && url.includes('activity_window_minutes=60'))).toBe(true);
  });

  test('applies per-agent filters through the shared brain scope query', async () => {
    localStorage.setItem('homie.hive.view', 'activity');
    const calls = mockBrainFetch();

    render(<HiveMind />);

    await waitFor(() => expect(screen.getByText('Research handoff activity')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: /research/i }));

    await waitFor(() => {
      expect(calls.some((url) => url.includes('/api/brain/graph') && url.includes('scope=persona') && url.includes('scope_id=research'))).toBe(true);
    });
  });

  test('renders durable memory as the 2D base graph with toggleable activity overlay', async () => {
    const calls = mockBrainFetch();
    const { container } = render(<HiveMind />);

    await waitFor(() => expect(screen.getByLabelText('Homie brain graph')).toBeInTheDocument());
    expect(screen.getByText('The Homie remembers the global brain.')).toBeInTheDocument();
    expect(screen.getByText(/Rendering 2\/2 loaded nodes and 1\/1 loaded links/i)).toBeInTheDocument();
    expect(screen.getByText(/Loaded 1\/1 matching memory chunks/i)).toBeInTheDocument();
    expect(screen.getByText('Relationships')).toBeInTheDocument();
    expect(container.querySelectorAll('.brain-activity-dot').length).toBeGreaterThan(0);

    fireEvent.click(screen.getAllByTitle('Activity overlay')[0]);
    await waitFor(() => expect(container.querySelectorAll('.brain-activity-dot').length).toBe(0));
    expect(calls.some((url) => url.includes('/api/brain/graph'))).toBe(true);
    expect(calls.some((url) => url.includes('limit=300') && url.includes('offset=0'))).toBe(true);
  });

  test('loads additional 2D audit graph pages without replacing existing nodes', async () => {
    const calls = mockBrainFetch({ hasMore: true });
    render(<HiveMind />);

    await waitFor(() => expect(screen.getByText('Load more graph')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Load more graph'));

    await waitFor(() => expect(screen.getAllByText('Follow-up memory').length).toBeGreaterThan(0));
    expect(screen.getByText(/Rendering 3\/3 loaded nodes and 2\/2 loaded links/i)).toBeInTheDocument();
    expect(calls.some((url) => url.includes('offset=1'))).toBe(true);
  });
});
