/**
 * agents-delete-button.test.tsx — F4 fix (PRD-8 Phase 3 post-build).
 *
 * Verifies the donor-shaped delete affordance per the
 * `framework_endpoint_delete_full` criterion:
 *
 *   (a) Clicking the delete button on an AgentCard triggers
 *       `window.confirm()`. On confirm, the resulting fetch URL contains
 *       BOTH `confirm=true` AND `expected_persona_id=<agent.id>` query
 *       parameters (URL search-params parsing, not naive substring).
 *   (b) Confirmation NOT included → backend returns 400 → UI shows error
 *       toast.
 *   (c) Confirmation included with WRONG `expected_persona_id` → backend
 *       returns 409 → UI shows mismatch error toast.
 *   (d) Default persona delete blocked at UI layer (button disabled);
 *       fetch never fires.
 *
 * The card uses `apiDelete()` from `lib/api.ts` which calls `fetch` with
 * `method: 'DELETE'` and a `Bearer <token>` header. We stub fetch and
 * inspect the URL.
 */

import { describe, test, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/preact';
import { AgentCard, type Agent } from '@/components/AgentCard';
import { toasts } from '@/lib/toasts';

// Helper to build a base agent.
function makeAgent(overrides: Partial<Agent> = {}): Agent {
  return {
    id: 'research',
    name: 'Research',
    description: 'Test agent',
    model: 'claude-sonnet-4-6',
    running: false,
    ...overrides,
  };
}

describe('agents delete button — confirm + expected_persona_id contract', () => {
  let originalConfirm: typeof window.confirm;

  beforeEach(() => {
    vi.restoreAllMocks();
    originalConfirm = window.confirm;
    // Drain the toast queue so each test asserts only its own emissions.
    toasts.value = [];
  });

  afterEach(() => {
    window.confirm = originalConfirm;
    toasts.value = [];
  });

  test('delete button includes confirm and expected_persona_id query params', async () => {
    const calls: { method: string; url: string }[] = [];
    globalThis.fetch = vi.fn(async (url: any, init: any) => {
      calls.push({ method: init?.method || 'GET', url: String(url) });
      return new Response(JSON.stringify({ deleted: true }), { status: 200 });
    }) as any;

    // Auto-confirm.
    window.confirm = vi.fn(() => true) as any;

    const onChange = vi.fn();
    render(<AgentCard agent={makeAgent({ id: 'research' })} onChange={onChange} />);

    // Locate the trash-icon delete button by title.
    const deleteBtn = screen.getByTitle('Delete');
    fireEvent.click(deleteBtn);

    await waitFor(() => {
      const deleteCall = calls.find((c) => c.method === 'DELETE');
      expect(deleteCall).toBeDefined();
    });

    const deleteCall = calls.find((c) => c.method === 'DELETE')!;
    // Use URL search-params parsing — NOT naive substring.
    // Tests run under happy-dom with origin http://localhost; fetch URLs
    // may be relative, so prefix with origin for URL parser.
    const parsed = new URL(deleteCall.url, 'http://localhost');
    expect(parsed.pathname).toMatch(/\/api\/agents\/research\/full$/);
    expect(parsed.searchParams.get('confirm')).toBe('true');
    expect(parsed.searchParams.get('expected_persona_id')).toBe('research');
  });

  test('confirmation NOT included → backend returns 400 → UI shows error toast with BACKEND message', async () => {
    const calls: { method: string; url: string }[] = [];
    globalThis.fetch = vi.fn(async (url: any, init: any) => {
      calls.push({ method: init?.method || 'GET', url: String(url) });
      // Backend returns 400 confirmation-required.
      return new Response(
        JSON.stringify({ error: 'confirmation required', hint: 'add ?confirm=true' }),
        { status: 400 },
      );
    }) as any;

    // Auto-confirm at the UI layer (the test simulates a stale UI that
    // forgot to add the query param — the BACKEND returns 400).
    window.confirm = vi.fn(() => true) as any;

    const onChange = vi.fn();
    render(<AgentCard agent={makeAgent({ id: 'broken-flow' })} onChange={onChange} />);

    const deleteBtn = screen.getByTitle('Delete');
    fireEvent.click(deleteBtn);

    // Wait for the fetch to fire and resolve to 400.
    await waitFor(() => {
      const c = calls.find((c) => c.method === 'DELETE');
      expect(c).toBeDefined();
    });

    // The 400 response causes apiDelete to throw an ApiError → catch
    // block in AgentCard.run() pushes a toast and never calls onChange.
    expect(onChange).not.toHaveBeenCalled();

    // NF1 fix: the toast must surface the BACKEND error detail, not the
    // generic `DELETE <path> failed: <status>` string. Assert the queue
    // captured the backend message + hint composition.
    await waitFor(() => {
      expect(toasts.value.length).toBeGreaterThan(0);
    });
    const errorToasts = toasts.value.filter((t) => t.tone === 'error');
    expect(errorToasts.length).toBeGreaterThan(0);
    const last = errorToasts[errorToasts.length - 1];
    expect(last.title).toBe('delete failed');
    expect(last.description).toContain('confirmation required');
    expect(last.description).toContain('add ?confirm=true');
    // Confirm we did NOT default to the generic error.
    expect(last.description).not.toMatch(/DELETE\s+\/api\/agents\/.*failed:\s*400/);
  });

  test('confirmation included with WRONG expected_persona_id → backend returns 409', async () => {
    const calls: { method: string; url: string }[] = [];
    globalThis.fetch = vi.fn(async (url: any, init: any) => {
      calls.push({ method: init?.method || 'GET', url: String(url) });
      // Simulates: UI sent the right query param shape, but the backend
      // detected expected_persona_id mismatch (e.g. operator pasted the
      // wrong id into a confirmation modal we'll add in Phase 4).
      return new Response(
        JSON.stringify({
          error: 'expected_persona_id mismatch',
          actual: 'broken-flow',
          expected: 'something-else',
        }),
        { status: 409 },
      );
    }) as any;

    window.confirm = vi.fn(() => true) as any;

    const onChange = vi.fn();
    render(<AgentCard agent={makeAgent({ id: 'broken-flow' })} onChange={onChange} />);

    const deleteBtn = screen.getByTitle('Delete');
    fireEvent.click(deleteBtn);

    await waitFor(() => {
      const c = calls.find((c) => c.method === 'DELETE');
      expect(c).toBeDefined();
    });

    // 409 path → onChange not called, toast emitted.
    expect(onChange).not.toHaveBeenCalled();

    // Sanity: the URL DID contain confirm=true (the UI is correct; the
    // BACKEND is what rejected). This guards against regression where
    // the UI silently strips the query param.
    const deleteCall = calls.find((c) => c.method === 'DELETE')!;
    const parsed = new URL(deleteCall.url, 'http://localhost');
    expect(parsed.searchParams.get('confirm')).toBe('true');

    // NF1 fix: backend mismatch detail must reach the toast. We send
    // {error, expected, actual} and the catch block composes
    // "<error> (actual=<actual>)" — the operator can SEE which id was
    // wrong instead of being told the generic "DELETE failed: 409".
    await waitFor(() => {
      expect(toasts.value.length).toBeGreaterThan(0);
    });
    const errorToasts = toasts.value.filter((t) => t.tone === 'error');
    expect(errorToasts.length).toBeGreaterThan(0);
    const last = errorToasts[errorToasts.length - 1];
    expect(last.title).toBe('delete failed');
    expect(last.description).toContain('expected_persona_id mismatch');
    expect(last.description).toContain('actual=broken-flow');
    expect(last.description).not.toMatch(/DELETE\s+\/api\/agents\/.*failed:\s*409/);
  });

  test('backend returns 403 default-profile reject → UI shows BACKEND message in toast', async () => {
    const calls: { method: string; url: string }[] = [];
    globalThis.fetch = vi.fn(async (url: any, init: any) => {
      calls.push({ method: init?.method || 'GET', url: String(url) });
      return new Response(
        JSON.stringify({ error: 'default profile cannot be deleted' }),
        { status: 403 },
      );
    }) as any;

    window.confirm = vi.fn(() => true) as any;

    const onChange = vi.fn();
    // Use a non-default id at the UI layer (so the button is enabled), but
    // the backend simulates a 403 — this exercises the toast-surfacing path
    // for the default-profile guard rather than the UI button-disabled path.
    render(<AgentCard agent={makeAgent({ id: 'research' })} onChange={onChange} />);

    const deleteBtn = screen.getByTitle('Delete');
    fireEvent.click(deleteBtn);

    await waitFor(() => {
      const c = calls.find((c) => c.method === 'DELETE');
      expect(c).toBeDefined();
    });
    expect(onChange).not.toHaveBeenCalled();

    await waitFor(() => {
      expect(toasts.value.length).toBeGreaterThan(0);
    });
    const errorToasts = toasts.value.filter((t) => t.tone === 'error');
    expect(errorToasts.length).toBeGreaterThan(0);
    const last = errorToasts[errorToasts.length - 1];
    expect(last.title).toBe('delete failed');
    expect(last.description).toContain('default profile cannot be deleted');
    expect(last.description).not.toMatch(/DELETE\s+\/api\/agents\/.*failed:\s*403/);
  });

  test('default persona delete blocked at UI layer (button disabled, fetch never fires)', async () => {
    const calls: { method: string; url: string }[] = [];
    globalThis.fetch = vi.fn(async (url: any, init: any) => {
      calls.push({ method: init?.method || 'GET', url: String(url) });
      return new Response('{}', { status: 200 });
    }) as any;

    const confirmSpy = vi.fn(() => true);
    window.confirm = confirmSpy as any;

    const onChange = vi.fn();
    // The browser side uses 'main' for the default persona id (Q4 lock).
    render(<AgentCard agent={makeAgent({ id: 'main', name: 'Main' })} onChange={onChange} />);

    const deleteBtn = screen.getByTitle('Delete') as HTMLButtonElement;
    expect(deleteBtn.disabled).toBe(true);

    // Even if a misbehaving operator forces a click, the disabled button
    // does not fire its onClick handler.
    fireEvent.click(deleteBtn);

    // No DELETE fetch call made.
    const deleteCalls = calls.filter((c) => c.method === 'DELETE');
    expect(deleteCalls.length).toBe(0);
    expect(onChange).not.toHaveBeenCalled();
  });

  test('user cancels the confirm() prompt → no fetch call', async () => {
    const calls: { method: string; url: string }[] = [];
    globalThis.fetch = vi.fn(async (url: any, init: any) => {
      calls.push({ method: init?.method || 'GET', url: String(url) });
      return new Response('{}', { status: 200 });
    }) as any;

    // User clicks Cancel.
    window.confirm = vi.fn(() => false) as any;

    const onChange = vi.fn();
    render(<AgentCard agent={makeAgent({ id: 'research' })} onChange={onChange} />);

    const deleteBtn = screen.getByTitle('Delete');
    fireEvent.click(deleteBtn);

    // confirm() returned false → run() returns early before any fetch.
    const deleteCalls = calls.filter((c) => c.method === 'DELETE');
    expect(deleteCalls.length).toBe(0);
    expect(onChange).not.toHaveBeenCalled();
  });
});
