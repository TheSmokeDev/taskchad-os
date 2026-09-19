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

  it.each([1789061169, null, 'not-a-timestamp'])('renders dispatcher Unix seconds or an explicit unknown (%s)', async (value) => {
    globalThis.fetch = vi.fn(async (input: string | URL | Request) => response(String(input).includes('/records?')
      ? { records: [], next_cursor: null }
      : { ...summary(), cognition: { cycles: {}, understanding: {}, investigations: {}, delivered_contexts: 0,
        dispatcher: { state: 'healthy', last_success_at: value } } })) as typeof fetch;
    render(<AgentLearning agentId="main" />);
    const expected = typeof value === 'number' ? new Date(value * 1000).toLocaleString() : 'Unknown';
    const check = await screen.findByText((text) => text.includes(`Last successful check: ${expected}`));
    expect(check).toBeInTheDocument();
    if (typeof value === 'number') {
      expect(check.textContent).toContain('2026');
      expect(check.textContent).not.toContain('1970');
    }
  });

  it('shows actual background provider failures without claiming learning finished', async () => {
    globalThis.fetch = vi.fn(async (input: string | URL | Request) => response(String(input).includes('/records?')
      ? { records: [], next_cursor: null }
      : { ...summary(), failures: 1, queue: { pending: 1, statuses: { retry: 1 }, jobs: [{ id: 'job', kind: 'candidate', stage: 'evaluate', status: 'retry', last_error: 'Provider temporarily unavailable' }] } })) as typeof fetch;
    render(<AgentLearning agentId="sales" />);
    await screen.findByText('Provider temporarily unavailable');
    expect(screen.getByText('evaluate · retry')).toBeInTheDocument();
  });

  it('shows exact synthesis provenance and tuning readiness while initial loads stay read-only', async () => {
    const actions: string[] = [];
    globalThis.fetch = vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const path = String(input);
      if (init?.method === 'POST') { actions.push(path); return response({ status: path.endsWith('/rollback') ? 'rolled_back' : 'queued' }); }
      return response(path.includes('/records?') ? { records: [], next_cursor: null } : {
        ...summary(),
        lifecycle: {
          persona_id: 'main', pending_scope: 'Admitted cycles only; unadmitted sources are not scanned by this read.',
          synthesis: { dream: { pending_cycles: 1, completed_cycles: 0, consumed_sources: 1, consumed_characters: 12,
            latest_request: { status: 'queued' }, pending_consumers: [{ cycle_id: 'dream-1', status: 'retained', input_count: 1 }] } },
          pending_stages: [{ id: 'job', kind: 'dream', stage: 'synthesis_support', status: 'deferred', reason: 'Provider unavailable' }],
          request_statuses: { queued: 1, no_signal: 2 },
          recent_cycles: [{ id: 'dream-1', synthesis_kind: 'dream', status: 'retained', conclusion: 'A tentative interpretation.',
            input_manifest: [{ ref: 'episode:sample', revision: 'revision-a', kind: 'episode', start: 0, end: 12, complete: false }],
            omitted_manifest: [{ start: 12, end: 30 }], consumption_status: 'consumed', projection_status: 'pending', partial_inputs: 1,
            result_ids: ['idea-1'], model_calls: [{ id: 'execution-1', provider: 'fake-provider', model: 'fake-model', status: 'executed' }] }],
        },
        tuning: { min_cases: 60, validated_cases: 8, development_cases: 0, heldout_cases: 0, readiness: 'not_ready', reason: 'insufficient_validated_cases',
          latest_run: { id: 'run-1', status: 'no_change' }, latest_evaluation: null, active_policy: { id: 'policy-1', status: 'active' }, policies: [{ id: 'policy-1' }] },
      });
    }) as typeof fetch;
    render(<AgentLearning agentId="main" />);
    await screen.findByText('Recall tuning');
    expect(screen.getByText('8 / 60')).toBeInTheDocument();
    expect(screen.getByText(/1 partial inputs · 1 omitted inputs/)).toBeInTheDocument();
    expect(screen.getByText(/Recorded model calls: 1 · fake-provider/)).toBeInTheDocument();
    expect(screen.getByText(/episode:sample · revision revision-a · \[0, 12\)/)).toBeInTheDocument();
    expect(actions).toEqual([]);
    fireEvent.click(screen.getByRole('button', { name: 'Request tuning run' }));
    await waitFor(() => expect(actions).toEqual(['/api/agents/main/learning/tuning/run']));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Roll back recall policy' })).not.toBeDisabled());
    fireEvent.click(screen.getByRole('button', { name: 'Roll back recall policy' }));
    expect(actions).toHaveLength(1);
    fireEvent.click(screen.getByRole('button', { name: 'Confirm recall rollback' }));
    await waitFor(() => expect(actions).toEqual(['/api/agents/main/learning/tuning/run', '/api/agents/main/learning/tuning/rollback']));
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

  it('shows understanding and investigation reports and only invokes reasoning explicitly', async () => {
    const requests: Array<{ path: string; method: string }> = [];
    globalThis.fetch = vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const path = String(input);
      requests.push({ path, method: init?.method ?? 'GET' });
      if (path.includes('/report?')) return response({
        persona_id: 'main', period: { since: '', until: '' }, counts: { distinct_conclusions: 2, understanding_changes: 3, investigations_opened: 1 },
        records: [{ id: 'idea_1', kind: 'understanding', title: 'Breakout follow-through' }],
        open_investigations: [{ id: 'inquiry_1', question: 'Did the next candle confirm it?', status: 'pending' }],
        has_activity: true, records_truncated: false, narrative_status: init?.method === 'POST' ? 'generated' : 'not_requested',
        narrative: init?.method === 'POST' ? 'The later candle changed my tentative interpretation [idea_1].' : null,
      });
      if (path.endsWith('/records/idea_1')) return response({ ...method, id: 'idea_1', kind: 'understanding', payload: { title: 'Breakout follow-through', content: 'Treat the first breakout as tentative.', uncertainty: 'One sample' } });
      return response(path.includes('/records?') ? { records: [], next_cursor: null } : {
        ...summary(), cognition: { cycles: { completed: 1 }, understanding: { tentative: 2 }, investigations: { pending: 1 }, delivered_contexts: 1,
          dispatcher: { state: 'healthy', adapter_coverage: { claude: 'engine_callbacks' } } },
      });
    }) as typeof fetch;
    render(<AgentLearning agentId="main" />);
    await screen.findByText('Distinct conclusions');
    expect(requests.every((request) => request.method === 'GET')).toBe(true);
    expect(screen.getByText(/Did the next candle confirm it/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Explain these changes' }));
    await screen.findByText(/The later candle changed my tentative interpretation/);
    expect(requests.filter((request) => request.method === 'POST')).toHaveLength(1);
    fireEvent.change(screen.getByLabelText('Learning report period'), { target: { value: '1' } });
    await waitFor(() => expect(screen.queryByText(/The later candle changed my tentative interpretation/)).not.toBeInTheDocument());
    fireEvent.click(await screen.findByRole('button', { name: /Breakout follow-through/ }));
    await screen.findByText('Treat the first breakout as tentative.');
    fireEvent.change(screen.getByLabelText('Filter learning history'), { target: { value: 'cognitive_cycle' } });
    await waitFor(() => expect(requests.some((request) => request.path.includes('kind=cognitive_cycle'))).toBe(true));
    fireEvent.change(screen.getByLabelText('Filter learning status'), { target: { value: 'completed' } });
    await waitFor(() => expect(requests.some((request) => request.path.includes('kind=cognitive_cycle&status=completed'))).toBe(true));
  });
});
