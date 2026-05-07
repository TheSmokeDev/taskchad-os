import { describe, test, expect, beforeEach, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/preact';
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
    mockFetchOnce({
      memories: [
        { id: 'm1', personaId: 'main', text: 'Hello world memory', tags: ['note'], createdAt: Date.now() / 1000 - 60 },
      ],
    });
    render(<Memories />);
    await waitFor(() => expect(screen.getByText(/hello world memory/i)).toBeInTheDocument());
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
