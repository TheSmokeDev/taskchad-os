import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/preact';
import { AgentLearning } from '@/components/AgentLearning';
import type { LearningRecord, LearningSummary } from '@/types/learning';

const method: LearningRecord = { id: 'act_1', persona_id: 'main', kind: 'activation', created_at: '2026-09-06T10:00:00Z', payload: { title: 'Clarify the objection', status: 'active_provisional', candidate_id: 'cand_1' } };
function summary(paused = false): LearningSummary {
  return { persona_id: 'main', enabled: true, paused, counts: { experience: 2 }, pending_outcomes: 1, active_methods: [method], failures: 0 };
}
function response(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

describe('persona learning operator panel', () => {
  afterEach(() => { cleanup(); vi.restoreAllMocks(); });

  it('shows actual background provider failures without claiming learning finished', async () => {
    globalThis.fetch = vi.fn(async (input: string | URL | Request) => response(String(input).includes('/records?')
      ? { records: [], next_cursor: null }
      : { ...summary(), failures: 1, queue: { pending: 1, statuses: { retry: 1 }, jobs: [{ id: 'job', kind: 'candidate', stage: 'evaluate', status: 'retry', last_error: 'Provider temporarily unavailable' }] } })) as typeof fetch;
    render(<AgentLearning agentId="sales" />);
    await screen.findByText('Provider temporarily unavailable');
    expect(screen.getByText('evaluate · retry')).toBeInTheDocument();
  });

  it('shows methods and evidence, and sends no action on initial render', async () => {
    const requests: Array<{ path: string; method: string }> = [];
    globalThis.fetch = vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const path = String(input);
      requests.push({ path, method: init?.method ?? 'GET' });
      if (path.endsWith('/records/act_1')) return response({ ...method, links: [{ id: 'cand_1', label: 'Supporting candidate' }] });
      if (path.endsWith('/records/cand_1')) return response({ ...method, id: 'cand_1', kind: 'candidate', payload: { title: 'Ask before discounting', content: '<script>unsafe()</script>' } });
      return response(path.includes('/records?') ? { records: [], next_cursor: null } : summary());
    }) as typeof fetch;
    const { container } = render(<AgentLearning agentId="main" />);
    await screen.findByRole('button', { name: /Clarify the objection/ });
    expect(screen.getByText(/does not establish live results/)).toBeInTheDocument();
    expect(requests.every((row) => row.method === 'GET')).toBe(true);
    fireEvent.click(screen.getByRole('button', { name: /Clarify the objection/ }));
    fireEvent.click(await screen.findByRole('button', { name: 'Supporting candidate' }));
    await screen.findByText('Ask before discounting');
    expect(container.querySelector('script')).toBeNull();
  });

  it('pauses and resumes through explicit buttons and refreshes physical status', async () => {
    let paused = false;
    const actions: string[] = [];
    globalThis.fetch = vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const path = String(input);
      if (init?.method === 'POST') {
        actions.push(path);
        paused = path.endsWith('/pause');
      }
      return response(path.includes('/records?') ? { records: [], next_cursor: null } : summary(paused));
    }) as typeof fetch;
    render(<AgentLearning agentId="main" />);
    fireEvent.click(await screen.findByRole('button', { name: 'Pause learning' }));
    await screen.findByText(/Learning is paused/);
    fireEvent.click(await screen.findByRole('button', { name: 'Resume learning' }));
    await waitFor(() => expect(screen.queryByText(/Learning is paused/)).not.toBeInTheDocument());
    expect(actions).toEqual(['/api/agents/main/learning/pause', '/api/agents/main/learning/resume']);
  });

  it('requires the rollback button and confirmation, then displays a conflict without hiding it', async () => {
    const actions: string[] = [];
    globalThis.fetch = vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const path = String(input);
      if (init?.method === 'POST') { actions.push(path); return response({ detail: 'Newer procedure conflicts with rollback' }, 409); }
      return response(path.includes('/records?') ? { records: [], next_cursor: null } : summary());
    }) as typeof fetch;
    render(<AgentLearning agentId="main" />);
    fireEvent.click(await screen.findByRole('button', { name: 'Roll back' }));
    expect(actions).toEqual([]);
    fireEvent.click(screen.getByRole('button', { name: 'Confirm rollback' }));
    await waitFor(() => expect(screen.getAllByText(/Newer procedure conflicts with rollback/).length).toBeGreaterThan(0));
    expect(actions).toEqual(['/api/agents/main/learning/activations/act_1/rollback']);
    expect(screen.getByRole('button', { name: 'Confirm rollback' })).toBeInTheDocument();
  });

  it('paginates with the returned cursor and resets the cursor when filtering', async () => {
    const urls: string[] = [];
    globalThis.fetch = vi.fn(async (input: string | URL | Request) => {
      const path = String(input); urls.push(path);
      return response(path.includes('/records?') ? { records: [method], next_cursor: 'before:8' } : summary());
    }) as typeof fetch;
    render(<AgentLearning agentId="sales" />);
    await waitFor(() => expect(screen.getByRole('button', { name: 'Older' })).not.toBeDisabled());
    fireEvent.click(screen.getByRole('button', { name: 'Older' }));
    await waitFor(() => expect(urls.some((url) => url.includes('cursor=before%3A8'))).toBe(true));
    fireEvent.change(screen.getByLabelText('Filter learning history'), { target: { value: 'evaluation' } });
    await waitFor(() => expect(urls.at(-1)).toBe('/api/agents/sales/learning/records?limit=30&kind=evaluation'));
  });

  it('distinguishes empty records, configuration disable, and unavailable service', async () => {
    globalThis.fetch = vi.fn(async (input: string | URL | Request) => response(String(input).includes('/records?') ? { records: [], next_cursor: null } : { ...summary(), enabled: false, active_methods: [] })) as typeof fetch;
    const first = render(<AgentLearning agentId="main" />);
    await screen.findByText('No learning records yet');
    expect(screen.getByRole('button', { name: 'Pause learning' })).toBeDisabled();
    expect(screen.getByText(/Learning is disabled/)).toBeInTheDocument();
    first.unmount();
    globalThis.fetch = vi.fn(async () => response({ detail: 'Learning service unavailable' }, 503));
    render(<AgentLearning agentId="main" />);
    await screen.findByText(/Learning status unavailable/);
    expect(screen.queryByText('No learning records yet')).not.toBeInTheDocument();
  });
});
