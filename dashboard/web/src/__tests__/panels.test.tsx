import { describe, test, expect, beforeEach, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/preact';
import { Agents } from '@/pages/Agents';
import { Memories } from '@/pages/Memories';
import { Scheduled } from '@/pages/Scheduled';
import { Usage } from '@/pages/Usage';

function mockFetchOnce(payload: unknown) {
  globalThis.fetch = vi.fn(async () =>
    new Response(JSON.stringify(payload), { status: 200, headers: { 'content-type': 'application/json' } }),
  ) as any;
}

describe('panels populate from fixture API responses', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  test('Agents page renders agent name from /api/agents', async () => {
    mockFetchOnce({
      agents: [
        { id: 'main', name: 'Homie', description: 'Default', model: 'claude-opus-4-7', running: true, todayTurns: 12, lane: 'claude_native', planQuotaPct: 8 },
      ],
    });
    render(<Agents />);
    await waitFor(() => expect(screen.getByText('Homie')).toBeInTheDocument());
  });

  test('Memories page renders memory text', async () => {
    globalThis.fetch = vi.fn(async (url: string) => {
      if (url.includes('/api/brain/graph')) {
        return new Response(JSON.stringify({
          nodes: [
            {
              id: 'chunk:1',
              label: 'Mission Control',
              kind: 'chunk',
              scope_type: 'global',
              scope_id: 'main',
              source_path: 'daily/2026-05-15.md',
              section_title: 'Mission Control',
              text: 'Hello world memory',
              tags: ['vault-chunk'],
              created_at: Date.now() / 1000 - 60,
            },
          ],
          edges: [],
          stats: { total_nodes: 1, total_edges: 0, total_chunks: 1 },
        }), { status: 200, headers: { 'content-type': 'application/json' } });
      }
      return new Response(JSON.stringify({
        memories: [
          {
            id: 1,
            persona_id: 'main',
            source_path: 'daily/2026-05-15.md',
            chunk_text: 'Hello world memory',
            tags: ['vault-chunk'],
            created_at: Date.now() / 1000 - 60,
          },
        ],
      }), { status: 200, headers: { 'content-type': 'application/json' } });
    }) as any;
    render(<Memories />);
    await waitFor(() => expect(screen.getByText(/hello world memory/i)).toBeInTheDocument());
    expect(screen.getByText(/daily\/2026-05-15\.md/i)).toBeInTheDocument();
  });

  test('Memories list tab renders memory rows', async () => {
    globalThis.fetch = vi.fn(async (url: string) => {
      if (url.includes('/api/brain/graph')) {
        return new Response(JSON.stringify({ nodes: [], edges: [], stats: {} }), {
          status: 200,
          headers: { 'content-type': 'application/json' },
        });
      }
      return new Response(JSON.stringify({
        memories: [
          {
            id: 1,
            persona_id: 'main',
            source_path: 'daily/2026-05-15.md',
            chunk_text: 'Hello world memory',
            tags: ['vault-chunk'],
            created_at: Date.now() / 1000 - 60,
          },
        ],
      }), { status: 200, headers: { 'content-type': 'application/json' } });
    }) as any;
    render(<Memories />);
    fireEvent.click(screen.getByRole('button', { name: /memory list/i }));
    await waitFor(() => expect(screen.getByText(/hello world memory/i)).toBeInTheDocument());
    expect(screen.getByText(/daily\/2026-05-15\.md/i)).toBeInTheDocument();
  });

  test('Scheduled page renders task prompt', async () => {
    mockFetchOnce({
      tasks: [
        { taskId: 't1', personaId: 'main', cron: '0 9 * * *', prompt: 'Daily standup', enabled: true },
      ],
    });
    render(<Scheduled />);
    await waitFor(() => expect(screen.getByText(/daily standup/i)).toBeInTheDocument());
  });

  test('Usage page renders lane-aware summary', async () => {
    mockFetchOnce({
      timeline: [],
      summary: {
        claude_native: { turns_today: 17, messages_today: 24, plan_quota_estimate_pct: 12 },
        generic: { by_provider: { 'openai-compatible': { cost_usd: 1.42, messages: 8, model: 'gpt-4o' } }, total_cost_usd: 1.42 },
      },
    });
    render(<Usage />);
    await waitFor(() => {
      // Both lane labels present (may appear multiple times — card title
      // + pill title attribute).
      expect(screen.getAllByText(/Claude Max/i).length).toBeGreaterThan(0);
      expect(screen.getAllByText(/Generic providers/i).length).toBeGreaterThan(0);
      // Both lane values present (turns + cost).
      expect(screen.getByText('17')).toBeInTheDocument();
    });
  });
});
