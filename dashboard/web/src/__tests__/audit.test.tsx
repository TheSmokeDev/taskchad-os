import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/preact';
import { Audit } from '@/pages/Audit';

function jsonResponse(body: unknown, status: number): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

describe('Audit page', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('renders the fail-closed empty state when the endpoint returns 503', async () => {
    globalThis.fetch = vi.fn(async () =>
      jsonResponse(
        { detail: 'DASHBOARD_ADMIN_TOKEN must be set; audit-log endpoint disabled' },
        503,
      ),
    ) as unknown as typeof fetch;

    render(<Audit />);

    expect(await screen.findByText('Audit requires DASHBOARD_ADMIN_TOKEN')).toBeInTheDocument();
  });

  it('renders a generic error state on other failures', async () => {
    globalThis.fetch = vi.fn(async () =>
      jsonResponse({ detail: 'admin bearer token required' }, 403),
    ) as unknown as typeof fetch;

    render(<Audit />);

    expect(await screen.findByText('Failed to load audit log')).toBeInTheDocument();
    expect(screen.getByText(/admin bearer token required/)).toBeInTheDocument();
  });

  it('renders audit rows defensively against the Python response shape', async () => {
    globalThis.fetch = vi.fn(async () =>
      jsonResponse(
        {
          rows: [
            {
              id: 2,
              created_at: '2026-06-08 10:00:00',
              operator_id: 'owner',
              persona_id: 'default',
              action: 'hard_delete',
              detail: '{"reason":"test"}',
              blocked: 0,
              target_persona_id: 'default',
              outcome: 'success',
            },
            {
              id: 1,
              created_at: '2026-06-08 09:00:00',
              persona_id: 'default',
              action: 'killswitch_refusal',
              blocked: 1,
            },
          ],
          next_before_id: 1,
        },
        200,
      ),
    ) as unknown as typeof fetch;

    render(<Audit />);

    expect(await screen.findByText('hard_delete')).toBeInTheDocument();
    expect(screen.getByText('owner')).toBeInTheDocument();
    expect(screen.getByText('success')).toBeInTheDocument();
    // Row without operator_id falls back to persona_id; without outcome,
    // blocked=1 renders as 'blocked'.
    expect(screen.getByText('killswitch_refusal')).toBeInTheDocument();
    expect(screen.getByText('blocked')).toBeInTheDocument();
  });
});
