import { describe, test, expect, beforeEach, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/preact';
import { Memories } from '@/pages/Memories';

function mockMemoryGraphFetch({ hasMore = false }: { hasMore?: boolean } = {}) {
  const calls: string[] = [];
  globalThis.fetch = vi.fn(async (url: string) => {
    calls.push(url);
    if (url.includes('/api/brain/graph')) {
      const offset = Number(new URL(url, 'http://localhost').searchParams.get('offset') ?? '0');
      const secondPage = offset > 0;
      return new Response(JSON.stringify({
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
            label: 'Audit continuation',
            kind: 'chunk',
            scope_type: 'global',
            scope_id: 'main',
            visibility: 'shared',
            source_path: 'MEMORY.md',
            section_title: 'Audit',
            text: 'Continuation page for full graph inspection.',
            tags: ['vault-chunk', 'global'],
          }] : [{
            id: 'room:cabinet-7',
            label: 'Strategy room',
            kind: 'session',
            scope_type: 'room',
            scope_id: 'cabinet-7',
            visibility: 'shared',
            text: 'text room with 7 transcript entries.',
            tags: ['cabinet', 'war-room'],
          }]),
        ],
        edges: [
          ...(secondPage
            ? [{ id: 'source:chunk:2->note:MEMORY.md', source: 'chunk:2', target: 'note:MEMORY.md', kind: 'source' }]
            : [
                { id: 'source:chunk:1->note:MEMORY.md', source: 'chunk:1', target: 'note:MEMORY.md', kind: 'source' },
                { id: 'scope:room:cabinet-7->note:MEMORY.md', source: 'room:cabinet-7', target: 'note:MEMORY.md', kind: 'scope' },
              ]),
        ],
        stats: {
          total_nodes: secondPage ? 2 : 3,
          total_edges: secondPage ? 1 : 2,
          total_chunks: hasMore ? 2 : 1,
          scope: 'all',
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
      }), { status: 200, headers: { 'content-type': 'application/json' } });
    }
    return new Response(JSON.stringify({ memories: [] }), {
      status: 200,
      headers: { 'content-type': 'application/json' },
    });
  }) as any;
  return calls;
}

function mockSemanticGraphFetch() {
  globalThis.fetch = vi.fn(async (url: string) => {
    if (url.includes('/api/brain/graph')) {
      return new Response(JSON.stringify({
        nodes: [
          {
            id: 'note:weekly/2026-W15.md',
            label: '2026-W15',
            kind: 'note',
            scope_type: 'global',
            scope_id: 'main',
            visibility: 'shared',
            source_path: 'weekly/2026-W15.md',
            tags: ['vault-note'],
            text: 'Weekly body.',
          },
          {
            id: 'note:Related Note.md',
            label: 'Related Note',
            kind: 'note',
            scope_type: 'global',
            scope_id: 'main',
            visibility: 'shared',
            source_path: 'Related Note.md',
            tags: ['vault-note'],
            text: 'Related body.',
          },
          {
            id: 'note:Missing Note.md',
            label: 'Missing Note',
            kind: 'note',
            scope_type: 'global',
            scope_id: 'main',
            visibility: 'shared',
            source_path: 'Missing Note.md',
            tags: ['vault-note', 'wikilink-target', 'unresolved-wikilink'],
          },
          {
            id: 'note:body.md',
            label: 'body',
            kind: 'note',
            scope_type: 'global',
            scope_id: 'main',
            visibility: 'shared',
            source_path: 'body.md',
            tags: ['vault-note'],
            text: 'Body link note.',
          },
        ],
        edges: [
          { id: 'related:note:weekly/2026-W15.md->note:Related Note.md', source: 'note:weekly/2026-W15.md', target: 'note:Related Note.md', kind: 'related', resolved: true, mention_count: 1, source_field: 'related' },
          { id: 'property:note:weekly/2026-W15.md->note:Missing Note.md', source: 'note:weekly/2026-W15.md', target: 'note:Missing Note.md', kind: 'property', resolved: false, mention_count: 1, source_field: 'superseded_by' },
          { id: 'wikilink:note:body.md->note:Related Note.md', source: 'note:body.md', target: 'note:Related Note.md', kind: 'wikilink', resolved: true, mention_count: 2, source_field: 'body' },
        ],
        stats: {
          total_nodes: 4,
          total_edges: 3,
          total_chunks: 0,
          memory: {
            total_chunks: 0,
            matching_chunks: 0,
            returned_chunks: 0,
            page: { limit: 300, offset: 0, returned_chunks: 0, matching_chunks: 0, has_more: false },
            vault_graph: {
              vault_notes: 3,
              vault_wikilink_edges: 3,
              vault_resolved_wikilink_edges: 2,
              vault_unresolved_wikilink_edges: 1,
              vault_wikilink_mentions: 4,
              vault_body_wikilink_edges: 1,
              vault_related_edges: 1,
              vault_property_wikilink_edges: 1,
            },
          },
        },
      }), { status: 200, headers: { 'content-type': 'application/json' } });
    }
    return new Response(JSON.stringify({ memories: [] }), {
      status: 200,
      headers: { 'content-type': 'application/json' },
    });
  }) as any;
}

describe('Memories graph page', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  test('renders memory graph stats and selected node details', async () => {
    mockMemoryGraphFetch();
    render(<Memories />);

    await waitFor(() => expect(screen.getByLabelText('Homie memory graph')).toBeInTheDocument());
    expect(screen.getByText('3')).toBeInTheDocument();
    expect(screen.getByText('The Homie remembers the global brain.')).toBeInTheDocument();
    expect(screen.getByText('MEMORY.md')).toBeInTheDocument();
    expect(screen.getByText(/Rendering 3\/3 loaded nodes and 2\/2 loaded links/i)).toBeInTheDocument();
  });

  test('opens room session nodes in the detail panel', async () => {
    mockMemoryGraphFetch();
    render(<Memories />);

    await waitFor(() => expect(screen.getAllByText('Strategy room').length).toBeGreaterThan(0));
    fireEvent.click(screen.getAllByRole('button', { name: /strategy room/i })[0]);
    expect(screen.getByText('text room with 7 transcript entries.')).toBeInTheDocument();
    expect(screen.getAllByText(/room\/cabinet-7/i).length).toBeGreaterThan(0);
  });

  test('makes selected note rows obvious and derives note previews from loaded chunks', async () => {
    mockMemoryGraphFetch();
    render(<Memories />);

    await waitFor(() => expect(screen.getByLabelText('Homie memory graph')).toBeInTheDocument());
    fireEvent.click(screen.getAllByRole('button', { name: /open memory node MEMORY/i })[0]);

    expect(screen.getByText('Selected memory node')).toBeInTheDocument();
    expect(screen.getByText(/Loaded chunk neighbors preview from 1 loaded chunk neighbor/i)).toBeInTheDocument();
    expect(screen.getAllByText(/The Homie remembers the global brain/i).length).toBeGreaterThan(0);
    expect(screen.getAllByRole('button', { name: /open memory node MEMORY/i }).some((button) => button.getAttribute('aria-pressed') === 'true')).toBe(true);
  });

  test('lets graph nodes be dragged while keeping selection obvious', async () => {
    mockMemoryGraphFetch();
    const { container } = render(<Memories />);

    const graph = await screen.findByLabelText('Homie memory graph');
    const noteNode = await waitFor(() => {
      const element = container.querySelector('[data-graph-node-id="note:MEMORY.md"]');
      expect(element).toBeTruthy();
      return element as Element;
    });
    const connectedChunk = await waitFor(() => {
      const element = container.querySelector('[data-graph-node-id="chunk:1"] circle');
      expect(element).toBeTruthy();
      return element as Element;
    });
    const chunkBefore = {
      x: connectedChunk.getAttribute('cx'),
      y: connectedChunk.getAttribute('cy'),
    };

    fireEvent.pointerDown(noteNode, { pointerId: 1, clientX: 120, clientY: 120 });
    fireEvent.pointerMove(graph, { pointerId: 1, clientX: 190, clientY: 170 });
    fireEvent.pointerUp(graph, { pointerId: 1, clientX: 190, clientY: 170 });

    expect(noteNode.getAttribute('data-selected')).toBe('true');
    expect(screen.getByText('Selected memory node')).toBeInTheDocument();
    await waitFor(() => {
      const currentChunk = container.querySelector('[data-graph-node-id="chunk:1"] circle');
      expect(currentChunk).toBeTruthy();
      expect({
        x: currentChunk!.getAttribute('cx'),
        y: currentChunk!.getAttribute('cy'),
      }).not.toEqual(chunkBefore);
    });
  });

  test('releases graph node drags from the global mouseup path', async () => {
    mockMemoryGraphFetch();
    const { container } = render(<Memories />);

    const graph = await screen.findByLabelText('Homie memory graph');
    const noteNode = await waitFor(() => {
      const element = container.querySelector('[data-graph-node-id="note:MEMORY.md"]');
      expect(element).toBeTruthy();
      return element as Element;
    });
    const connectedChunk = await waitFor(() => {
      const element = container.querySelector('[data-graph-node-id="chunk:1"] circle');
      expect(element).toBeTruthy();
      return element as Element;
    });
    const chunkBefore = {
      x: connectedChunk.getAttribute('cx'),
      y: connectedChunk.getAttribute('cy'),
    };

    fireEvent.pointerDown(noteNode, { pointerId: 2, clientX: 120, clientY: 120 });
    fireEvent.pointerMove(graph, { pointerId: 2, clientX: 210, clientY: 160 });
    fireEvent.mouseUp(window, { clientX: 210, clientY: 160 });

    expect(noteNode.getAttribute('data-selected')).toBe('true');
    await waitFor(() => {
      const currentChunk = container.querySelector('[data-graph-node-id="chunk:1"] circle');
      expect(currentChunk).toBeTruthy();
      expect({
        x: currentChunk!.getAttribute('cx'),
        y: currentChunk!.getAttribute('cy'),
      }).not.toEqual(chunkBefore);
    });
  });

  test('ignores mismatched pointer releases while dragging nodes', async () => {
    mockMemoryGraphFetch();
    const { container } = render(<Memories />);

    const graph = await screen.findByLabelText('Homie memory graph');
    const noteNode = await waitFor(() => {
      const element = container.querySelector('[data-graph-node-id="note:MEMORY.md"]');
      expect(element).toBeTruthy();
      return element as Element;
    });

    fireEvent.pointerDown(noteNode, { pointerId: 7, clientX: 120, clientY: 120 });
    fireEvent.pointerMove(graph, { pointerId: 7, clientX: 190, clientY: 170 });
    fireEvent.pointerUp(graph, { pointerId: 99, clientX: 190, clientY: 170 });

    expect(graph.getAttribute('class')).toContain('cursor-grabbing');

    fireEvent.pointerUp(graph, { pointerId: 7, clientX: 190, clientY: 170 });
    await waitFor(() => expect(graph.getAttribute('class')).not.toContain('cursor-grabbing'));
  });

  test('filters relationship kinds and focuses a selected node neighborhood', async () => {
    mockMemoryGraphFetch();
    render(<Memories />);

    await waitFor(() => expect(screen.getByLabelText('Homie memory graph')).toBeInTheDocument());

    fireEvent.click(screen.getByRole('button', { name: /show scope relationships/i }));
    expect(screen.getByText(/Rendering 3\/3 loaded nodes and 1\/2 loaded links/i)).toBeInTheDocument();
    expect(screen.getByText(/Filtered to Scope relationships/i)).toBeInTheDocument();

    fireEvent.click(screen.getAllByRole('button', { name: /strategy room/i })[0]);
    fireEvent.click(screen.getByRole('button', { name: /focus neighborhood/i }));

    expect(screen.getByText(/Rendering 2\/3 loaded nodes and 1\/2 loaded links/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /clear neighborhood focus/i })).toBeInTheDocument();
    expect(screen.getAllByText(/Relationships/i).length).toBeGreaterThan(0);
  });

  test('surfaces semantic vault link types and broken Obsidian targets', async () => {
    mockSemanticGraphFetch();
    render(<Memories />);

    await waitFor(() => expect(screen.getByLabelText('Homie memory graph')).toBeInTheDocument());
    expect(screen.getByText(/3 vault notes .* 2 resolved links .* 1 broken/i)).toBeInTheDocument();
    expect(screen.getByText(/1 related .* 1 body .* 1 property/i)).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /show related relationships/i }));
    expect(screen.getByText(/Rendering 4\/4 loaded nodes and 1\/3 loaded links/i)).toBeInTheDocument();
    expect(screen.getByText(/Filtered to Related relationships/i)).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /show broken relationships/i }));
    expect(screen.getByText(/Rendering 4\/4 loaded nodes and 1\/3 loaded links/i)).toBeInTheDocument();
    expect(screen.getByText(/Filtered to Broken relationships/i)).toBeInTheDocument();
    expect(screen.getAllByText('Broken').length).toBeGreaterThan(0);
  });

  test('requests the main persona overlay through scope_id=main', async () => {
    const calls = mockMemoryGraphFetch();
    render(<Memories />);

    await waitFor(() => expect(screen.getAllByText('Core memory').length).toBeGreaterThan(0));
    fireEvent.click(screen.getByRole('button', { name: /scope main/i }));

    await waitFor(() => {
      expect(calls.some((url) => url.includes('/api/brain/graph') && url.includes('scope=persona') && url.includes('scope_id=main'))).toBe(true);
    });
  });

  test('loads additional memory graph pages for audit inspection', async () => {
    const calls = mockMemoryGraphFetch({ hasMore: true });
    render(<Memories />);

    await waitFor(() => expect(screen.getByText('Load more graph')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Load more graph'));

    await waitFor(() => expect(screen.getAllByText('Audit continuation').length).toBeGreaterThan(0));
    expect(screen.getByText(/Rendering 4\/4 loaded nodes and 3\/3 loaded links/i)).toBeInTheDocument();
    expect(calls.some((url) => url.includes('offset=1'))).toBe(true);
  });
});
