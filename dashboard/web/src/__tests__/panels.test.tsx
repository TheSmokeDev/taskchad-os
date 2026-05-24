import { describe, test, expect, beforeEach, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/preact';
import { Agents } from '@/pages/Agents';
import { Memories } from '@/pages/Memories';
import { Scheduled } from '@/pages/Scheduled';
import { Usage } from '@/pages/Usage';
import { Jarvis } from '@/pages/Jarvis';

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

  test('Jarvis page renders runtime, autonomy, channel, and trace truth', async () => {
    mockFetchOnce({
      status: 'ok',
      timestamp: '2026-05-23T23:47:00Z',
      runtime: {
        selected_lane: 'claude_native',
        selected_model: 'claude-sonnet-4-6',
        selected_generic_provider: 'codex',
        generic_text_route: ['codex', 'gemini'],
        generic_tool_route: ['claude_native', 'codex'],
        configured_models: { claude_native: 'claude-sonnet-4-6' },
        providers: { claude_native: 'ready', codex: 'ready' },
      },
      autonomy: {
        autonomy_overall: 'live',
        autonomous_loop_overall: 'live',
        cognitive_loop_overall: 'live',
        source_wiring_overall: 'live',
      },
      memory: { doc_count: 2932, embedding_status: 'ready' },
      channels: {
        telegram: {
          connected: true,
          sessions_active: 1,
          metadata_alignment: {
            runtime_providers_populated: true,
            memory_doc_count_matches_cli: true,
          },
        },
        mission_control_relay: {
          health_check_port: 8787,
          orchestration_api_port: 4322,
        },
      },
      capabilities: {
        enabled_count: 7,
        total_count: 9,
        toolsets: ['google', 'telegram'],
        enabled: [
          { id: 'telegram_bot', display_name: 'Telegram Bot', source: 'direct' },
        ],
      },
      observability: {
        lookup_status: 'documented_local_proof',
        langfuse_trace_id: '34723c42e7103e986274c4825b0e68a3',
        sentry_event_id: 'f822285b539e4820bd50988bc7ec6984',
        self_amendment_proposal_id: '0b1f70e3-1d2d-4275-85b8-5aafa4ae8f7d',
      },
    });

    render(<Jarvis />);

    await waitFor(() => expect(screen.getByText('claude-sonnet-4-6')).toBeInTheDocument());
    expect(screen.getByText('2932')).toBeInTheDocument();
    expect(screen.getByText('Telegram Bot')).toBeInTheDocument();
    expect(screen.getByText('34723c42e7103e986274c4825b0e68a3')).toBeInTheDocument();
    expect(screen.getByText('f822285b539e4820bd50988bc7ec6984')).toBeInTheDocument();
  });
});
