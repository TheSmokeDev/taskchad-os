import { describe, test, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/preact';
import { AgentCreateWizard } from '@/components/AgentCreateWizard';

interface RecordedCall {
  method: string;
  url: string;
  body: any;
}

const PREVIEW_HASH = 'a'.repeat(64);
const STATE_HASH = 'b'.repeat(64);

function installFetchRecorder(calls: RecordedCall[]) {
  globalThis.fetch = vi.fn(async (url: any, init: any) => {
    const u = String(url);
    const body = init?.body ? JSON.parse(init.body) : null;
    calls.push({ method: init?.method || 'GET', url: u, body });

    if (u.includes('/api/agents/validate-id')) {
      return new Response(JSON.stringify({ valid: true, reason: null }), { status: 200 });
    }
    if (u.includes('/api/agents/templates')) {
      return new Response(JSON.stringify({
        templates: [
          {
            id: 'general-specialist',
            name: 'Specialist',
            description: 'Safe general specialist.',
            default_role: 'Handle scoped operator requests.',
            default_model: 'claude-sonnet-4-7',
            domain: 'general',
          },
          {
            id: 'ai-engineer',
            name: 'AI Engineer',
            description: 'Read-oriented engineer.',
            default_role: 'Inspect repositories and propose work.',
            default_model: 'claude-sonnet-4-7',
            domain: 'ai-engineering',
          },
        ],
      }), { status: 200 });
    }
    if (u.endsWith('/api/agents/preview') && init?.method === 'POST') {
      return new Response(JSON.stringify({
        persona_id: 'research',
        preview_hash: PREVIEW_HASH,
        state_hash: STATE_HASH,
        plan: {},
      }), { status: 200 });
    }
    if (u.endsWith('/api/agents') && init?.method === 'POST') {
      return new Response(JSON.stringify({
        persona_id: 'research',
        path: '/home/test/.homie/profiles/research',
        status: 'created',
        preview_hash: PREVIEW_HASH,
        receipt: { transaction_id: 'tx-123', outcome: 'created' },
      }), { status: 200 });
    }
    return new Response('{}', { status: 200 });
  }) as any;
}

describe('AgentCreateWizard — blueprint creation contract', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  test('previews and applies every authored field through canonical routes', async () => {
    const calls: RecordedCall[] = [];
    installFetchRecorder(calls);

    render(<AgentCreateWizard open={true} onClose={() => {}} onCreated={() => {}} />);

    fireEvent.input(screen.getByPlaceholderText('research'), {
      target: { value: 'research' },
    });
    fireEvent.input(screen.getByPlaceholderText(/competitive intel/i), {
      target: { value: 'Review architecture and propose implementation work' },
    });

    await waitFor(() => expect(screen.getByText(/Available/i)).toBeInTheDocument(), {
      timeout: 2000,
    });
    await waitFor(() => expect(screen.getByText('AI Engineer')).toBeInTheDocument());

    const selects = screen.getAllByRole('combobox') as HTMLSelectElement[];
    fireEvent.change(selects[1], { target: { value: 'ai-engineer' } });

    const next = screen.getByRole('button', { name: /Next: Channel/i });
    await waitFor(() => expect(next).not.toBeDisabled());
    fireEvent.click(next);

    fireEvent.input(screen.getByPlaceholderText('123456789012345678'), {
      target: { value: '123456789012345678' },
    });
    const create = screen.getByRole('button', { name: /Create Agent/i });
    expect(create).not.toBeDisabled();
    fireEvent.click(create);

    await waitFor(() => expect(screen.getByText(/Agent created/i)).toBeInTheDocument());

    const previewCall = calls.find(
      (call) => call.method === 'POST' && call.url.endsWith('/api/agents/preview'),
    );
    expect(previewCall?.body).toMatchObject({
      persona_id: 'research',
      display_name: 'AI Engineer',
      template: 'ai-engineer',
      role: 'Review architecture and propose implementation work',
      model: 'claude-sonnet-4-7',
      domain: 'ai-engineering',
      channel_intent: {
        kind: 'discord',
        channel_id: '123456789012345678',
        name: 'research',
      },
      operator_exec: false,
    });

    const createCall = calls.find(
      (call) => call.method === 'POST' && call.url.match(/\/api\/agents$/),
    );
    expect(createCall?.body).toMatchObject({
      ...previewCall?.body,
      expected_preview_hash: PREVIEW_HASH,
      expected_state_hash: STATE_HASH,
    });
    expect(calls.some((call) => call.url.includes('/api/agents/create'))).toBe(false);
    expect(calls.some((call) => call.url.includes('/api/agents/validate-token'))).toBe(false);
    expect(screen.getByText(PREVIEW_HASH)).toBeInTheDocument();
    expect(screen.getByText('tx-123')).toBeInTheDocument();
  });

  test('hostile non-digit channel intent blocks apply', async () => {
    const calls: RecordedCall[] = [];
    installFetchRecorder(calls);

    render(<AgentCreateWizard open={true} onClose={() => {}} onCreated={() => {}} />);
    fireEvent.input(screen.getByPlaceholderText('research'), {
      target: { value: 'research' },
    });
    fireEvent.input(screen.getByPlaceholderText(/competitive intel/i), {
      target: { value: 'Deep research' },
    });
    await waitFor(() => expect(screen.getByText(/Available/i)).toBeInTheDocument(), {
      timeout: 2000,
    });
    fireEvent.click(screen.getByRole('button', { name: /Next: Channel/i }));
    fireEvent.input(screen.getByPlaceholderText('123456789012345678'), {
      target: { value: '123abc' },
    });

    expect(
      screen.getByText('Discord channel ids contain digits only.'),
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Create Agent/i })).toBeDisabled();
    expect(calls.some((call) => call.url.endsWith('/api/agents/preview'))).toBe(false);
  });
});
