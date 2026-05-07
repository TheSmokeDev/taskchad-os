/**
 * agent-create-wizard.test.tsx — F1 fix (PRD-8 Phase 3 post-build).
 *
 * Asserts the wizard talks to the Phase 3 contract end-to-end:
 *   - validate-id: POST body `{persona_id}` (NOT GET, NOT `{id}`)
 *   - validate-token: POST body `{bot_token}` (NOT `{token}`); reads
 *     `{valid, username}` (NOT `{ok, botInfo}`)
 *   - create: POST `/api/agents` body `{persona_id, display_name,
 *     bot_token_env, model}` (NOT `{id, name, description, ...}`)
 *   - response: reads `{persona_id, path, status}` (NOT
 *     `{agentId, envKey, agentDir}`)
 *
 * The previous version was a gas-station test — it only checked URL
 * existence and let the wizard send donor-shaped bodies. Adversarial
 * post-build review caught this. This rewrite asserts METHOD, BODY shape,
 * and RESPONSE field consumption against the canonical surface.
 */

import { describe, test, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/preact';
import { AgentCreateWizard } from '@/components/AgentCreateWizard';

interface RecordedCall {
  method: string;
  url: string;
  body: any;
}

describe('AgentCreateWizard — Phase 3 contract', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  test('full wizard flow uses canonical Phase 3 endpoints, methods, and body shapes', async () => {
    const calls: RecordedCall[] = [];
    globalThis.fetch = vi.fn(async (url: any, init: any) => {
      const u = String(url);
      let parsedBody: any = null;
      if (init?.body) {
        try { parsedBody = JSON.parse(init.body); } catch { parsedBody = init.body; }
      }
      calls.push({ method: init?.method || 'GET', url: u, body: parsedBody });

      if (u.includes('/api/agents/validate-id')) {
        // Phase 3 contract response shape: {valid, reason}.
        return new Response(JSON.stringify({ valid: true, reason: null }), { status: 200 });
      }
      if (u.includes('/api/agents/validate-token')) {
        // Phase 3 contract response shape: {valid, display_name, username, error}.
        return new Response(
          JSON.stringify({
            valid: true,
            display_name: 'Research Bot',
            username: 'research_bot',
            error: null,
          }),
          { status: 200 },
        );
      }
      if (u.includes('/api/agents/templates')) {
        return new Response(JSON.stringify({ templates: [] }), { status: 200 });
      }
      if (u.endsWith('/api/agents') && init?.method === 'POST') {
        // Phase 3 contract response shape: {persona_id, path, status}.
        return new Response(
          JSON.stringify({
            persona_id: 'research',
            path: '/home/test/.homie/profiles/research',
            status: 'created',
          }),
          { status: 200 },
        );
      }
      return new Response('{}', { status: 200 });
    }) as any;

    render(<AgentCreateWizard open={true} onClose={() => {}} onCreated={() => {}} />);

    // Step 1 — fill basics.
    const idInput = screen.getByPlaceholderText('research') as HTMLInputElement;
    fireEvent.input(idInput, { target: { value: 'research' } });
    const descInput = screen.getByPlaceholderText(/competitive intel/i) as HTMLTextAreaElement;
    fireEvent.input(descInput, { target: { value: 'Deep research' } });

    // Wait for debounced validate-id to resolve and surface "Available".
    await waitFor(
      () => {
        expect(screen.getByText(/Available/i)).toBeInTheDocument();
      },
      { timeout: 2000 },
    );

    // Verify validate-id was POST (NOT GET) with body `{persona_id}`.
    const validateIdCalls = calls.filter((c) => c.url.includes('/api/agents/validate-id'));
    expect(validateIdCalls.length).toBeGreaterThan(0);
    const lastValidateId = validateIdCalls[validateIdCalls.length - 1];
    expect(lastValidateId.method).toBe('POST');
    expect(lastValidateId.body).toMatchObject({ persona_id: 'research' });
    // The donor shape was a GET with `?id=...` — must NEVER be sent.
    expect(lastValidateId.url).not.toMatch(/\?id=/);

    // Click Next.
    const nextBtn = screen.getByRole('button', { name: /Next: Bot token/i });
    await waitFor(() => expect(nextBtn).not.toBeDisabled());
    fireEvent.click(nextBtn);

    // Step 2 — paste token.
    const tokenInput = screen.getByPlaceholderText(/123456789/) as HTMLInputElement;
    fireEvent.input(tokenInput, { target: { value: '12345:abc' } });

    await waitFor(
      () => {
        expect(screen.getByText(/Verified/i)).toBeInTheDocument();
      },
      { timeout: 2000 },
    );

    // Verify validate-token was POST with body `{bot_token}` (NOT `{token}`).
    const validateTokenCalls = calls.filter((c) =>
      c.url.includes('/api/agents/validate-token'),
    );
    expect(validateTokenCalls.length).toBeGreaterThan(0);
    const lastValidateToken = validateTokenCalls[validateTokenCalls.length - 1];
    expect(lastValidateToken.method).toBe('POST');
    expect(lastValidateToken.body).toMatchObject({ bot_token: '12345:abc' });
    // Donor sent `{token: ...}` — must NEVER appear.
    expect(lastValidateToken.body).not.toHaveProperty('token');

    // Username appears in the verified-row text. Use getAllByText because
    // the suggested-bot-username helper (`homie_research_bot`) renders the
    // same string in step 2's BotFather instructions; the verified row is
    // a distinct element with leading "@" on the username.
    const verifiedRow = screen.getByText(/✓ Verified:/);
    expect(verifiedRow.textContent).toContain('research_bot');

    // Click Create.
    const createBtn = screen.getByRole('button', { name: /Create Agent/i });
    await waitFor(() => expect(createBtn).not.toBeDisabled());
    fireEvent.click(createBtn);

    await waitFor(() => {
      const postAgents = calls.find(
        (c) => c.method === 'POST' && c.url.match(/\/api\/agents$/),
      );
      expect(postAgents).toBeDefined();
    });

    // Donor URL alias `/api/agents/create` must NEVER appear.
    const donorAliasCalls = calls.filter((c) => c.url.includes('/api/agents/create'));
    expect(donorAliasCalls.length).toBe(0);

    // Verify create body uses Phase 3 field names.
    const createCall = calls.find(
      (c) => c.method === 'POST' && c.url.match(/\/api\/agents$/),
    )!;
    expect(createCall.body).toMatchObject({
      persona_id: 'research',
      display_name: expect.any(String),
      bot_token_env: expect.any(String),
      model: expect.any(String),
    });
    // bot_token_env is the env-var NAME, not the token VALUE — must NEVER
    // contain the literal token string.
    expect(createCall.body.bot_token_env).not.toContain('12345:abc');

    // Donor field names must NEVER appear in the create body.
    const donorFields = ['id', 'name', 'description', 'template', 'bot_token'];
    for (const f of donorFields) {
      expect(createCall.body).not.toHaveProperty(f);
    }

    // Step 3 — verify the wizard read the Phase 3 response shape.
    // The success panel shows `id: research` and `path: ...`.
    await waitFor(() => {
      expect(screen.getByText(/Agent created/i)).toBeInTheDocument();
    });
    // The created persona id is visible — search for the canonical
    // `path:` row to anchor the success panel content.
    expect(screen.getByText(/\/home\/test\/\.homie\/profiles\/research/)).toBeInTheDocument();
    // Donor field names (envKey/agentDir) should NOT appear in the panel.
    expect(screen.queryByText(/envKey/)).toBeNull();
    expect(screen.queryByText(/agentDir/)).toBeNull();
  });

  test('canonical create route is POST /api/agents (NOT /api/agents/create)', async () => {
    // Tighter regression test for the F1 root cause — donor used a URL
    // alias the wizard must never emit.
    const calls: string[] = [];
    globalThis.fetch = vi.fn(async (url: any, init: any) => {
      const u = String(url);
      calls.push(`${init?.method || 'GET'} ${u}`);
      if (u.includes('/api/agents/validate-id')) {
        return new Response(JSON.stringify({ valid: true, reason: null }), { status: 200 });
      }
      if (u.includes('/api/agents/validate-token')) {
        return new Response(
          JSON.stringify({ valid: true, username: 'test_bot', display_name: 'Test', error: null }),
          { status: 200 },
        );
      }
      if (u.includes('/api/agents/templates')) {
        return new Response(JSON.stringify({ templates: [] }), { status: 200 });
      }
      if (u.endsWith('/api/agents') && init?.method === 'POST') {
        return new Response(
          JSON.stringify({ persona_id: 'research', path: '/p', status: 'created' }),
          { status: 200 },
        );
      }
      return new Response('{}', { status: 200 });
    }) as any;

    render(<AgentCreateWizard open={true} onClose={() => {}} onCreated={() => {}} />);

    const idInput = screen.getByPlaceholderText('research') as HTMLInputElement;
    fireEvent.input(idInput, { target: { value: 'research' } });
    const descInput = screen.getByPlaceholderText(/competitive intel/i) as HTMLTextAreaElement;
    fireEvent.input(descInput, { target: { value: 'Deep research' } });

    await waitFor(() => expect(screen.getByText(/Available/i)).toBeInTheDocument(), { timeout: 2000 });
    const nextBtn = screen.getByRole('button', { name: /Next: Bot token/i });
    await waitFor(() => expect(nextBtn).not.toBeDisabled());
    fireEvent.click(nextBtn);

    const tokenInput = screen.getByPlaceholderText(/123456789/) as HTMLInputElement;
    fireEvent.input(tokenInput, { target: { value: '12345:abc' } });
    await waitFor(() => expect(screen.getByText(/Verified/i)).toBeInTheDocument(), { timeout: 2000 });

    const createBtn = screen.getByRole('button', { name: /Create Agent/i });
    await waitFor(() => expect(createBtn).not.toBeDisabled());
    fireEvent.click(createBtn);

    await waitFor(() => {
      const postCalls = calls.filter((c) => c.startsWith('POST'));
      expect(postCalls.find((c) => c.includes('/api/agents/create'))).toBeUndefined();
      expect(postCalls.find((c) => c.match(/POST .*\/api\/agents$/))).toBeDefined();
    });
  });
});
