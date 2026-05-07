import { describe, test, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/preact';
import { Usage } from '@/pages/Usage';
import { Agents } from '@/pages/Agents';

const LANE_AWARE_FIXTURE = {
  timeline: [
    { date: '2026-05-01', claude_native: { turns: 5, messages: 10 }, generic: { by_provider: { openai: { cost_usd: 0.5, messages: 3, model: 'gpt-4o' } }, total_cost_usd: 0.5 } },
    { date: '2026-05-02', claude_native: { turns: 8, messages: 16 }, generic: { by_provider: {}, total_cost_usd: 0 } },
  ],
  summary: {
    claude_native: { turns_today: 23, messages_today: 41, plan_quota_estimate_pct: 14 },
    generic: { by_provider: { openai: { cost_usd: 0.5, messages: 3, model: 'gpt-4o' } }, total_cost_usd: 0.5 },
  },
};

describe('donor pages respect lane-aware response shape', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  test('Usage renders BOTH claude_native turns AND generic.by_provider — never sums them', async () => {
    globalThis.fetch = vi.fn(async () => new Response(JSON.stringify(LANE_AWARE_FIXTURE), { status: 200 })) as any;
    render(<Usage />);

    await waitFor(() => {
      // claude_native lane: turns visible
      expect(screen.getByText('23')).toBeInTheDocument();
      // generic lane: provider name visible (multiple occurrences OK).
      expect(screen.getAllByText(/openai/).length).toBeGreaterThan(0);
      // Both lane labels present (multiple occurrences OK — card titles
      // + pill title tooltips).
      expect(screen.getAllByText(/Claude Max/i).length).toBeGreaterThan(0);
      expect(screen.getAllByText(/Generic providers/i).length).toBeGreaterThan(0);
    });

    // CRITICAL: never display a summed cost across lanes. The page must
    // never claim "$X.XX total today" combining Max + generic.
    // turns_today=23 must never be coerced to USD anywhere.
    const summed = `$${(0 + 0.5).toFixed(2)} (23 turns)`;
    expect(screen.queryByText(summed)).toBeNull();
  });

  test('Agents page bulk model selector calls PATCH /api/agents/model (global)', async () => {
    const calls: { method: string; url: string; body?: any }[] = [];
    globalThis.fetch = vi.fn(async (url: any, init: any) => {
      const u = String(url);
      calls.push({ method: init?.method || 'GET', url: u, body: init?.body ? JSON.parse(init.body) : undefined });
      if (u.includes('/api/agents/model') && init?.method === 'PATCH') {
        return new Response(JSON.stringify({ ok: true, updated: ['main'], restartRequired: [] }), { status: 200 });
      }
      if (u.includes('/api/agents/model')) {
        return new Response(JSON.stringify({ model: 'claude-opus-4-7' }), { status: 200 });
      }
      if (u.endsWith('/api/agents')) {
        return new Response(JSON.stringify({ agents: [] }), { status: 200 });
      }
      return new Response('{}', { status: 200 });
    }) as any;

    render(<Agents />);

    await waitFor(() => screen.getByText(/Global model/i));
    const select = screen.getByRole('combobox') as HTMLSelectElement;
    fireEvent.change(select, { target: { value: 'claude-haiku-4-5' } });

    await waitFor(() => {
      const patch = calls.find((c) => c.method === 'PATCH' && c.url.includes('/api/agents/model'));
      expect(patch).toBeDefined();
      expect(patch?.body).toMatchObject({ model: 'claude-haiku-4-5' });
    });
  });
});
