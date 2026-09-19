import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/preact';
import { AgentDetail } from '@/pages/AgentDetail';

vi.mock('wouter-preact', () => ({ useRoute: () => [true, { id: 'main' }] }));
vi.mock('@/components/AgentActions', () => ({ AgentActions: () => <div>Agent actions</div> }));
vi.mock('@/components/AvatarUploader', () => ({ AvatarUploader: () => <div>Avatar controls</div> }));

function response(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

describe('default persona learning bootstrap', () => {
  afterEach(() => { cleanup(); vi.restoreAllMocks(); });

  it('opens Learning when optional detail config is absent', async () => {
    globalThis.fetch = vi.fn(async (input: string | URL | Request) => {
      const path = String(input);
      if (path === '/api/agents/main') return response({ detail: 'Optional profile config missing' }, 404);
      if (path.includes('/learning/records?')) return response({ records: [], next_cursor: null });
      return response({ persona_id: 'main', enabled: true, paused: false, initialized: false, counts: {}, pending_outcomes: 0, active_methods: [], failures: 0 });
    }) as typeof fetch;
    render(<AgentDetail />);
    await screen.findByText('Agent details unavailable');
    fireEvent.click(screen.getByRole('button', { name: 'Learning' }));
    await screen.findByRole('button', { name: 'Pause learning' });
    expect(screen.queryByText('Agent actions')).not.toBeInTheDocument();
  });

  it('shows independent Learning denial instead of fabricating profile access', async () => {
    globalThis.fetch = vi.fn(async () => response({ detail: 'Persona access denied' }, 403)) as typeof fetch;
    render(<AgentDetail />);
    fireEvent.click(screen.getByRole('button', { name: 'Learning' }));
    expect((await screen.findAllByText(/Persona access denied/)).length).toBeGreaterThan(0);
    expect(screen.queryByRole('button', { name: 'Pause learning' })).not.toBeInTheDocument();
  });
});
