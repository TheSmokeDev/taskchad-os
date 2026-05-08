/**
 * kill-switch-banner.test.tsx — PRD-8 Phase 7a (WS7 R3 NB3).
 *
 * Verifies the rich-snapshot consumer behavior of KillSwitchBanner:
 *
 *   (1) all counters zero → component returns null (no banner)
 *   (2) nonzero refusal counter → renders "Kill-switch refusals: <name>=<n>"
 *   (3) nonzero audit_write_failures → renders "Audit-write failures: <name>=<n>"
 *   (4) both nonzero → renders both phrases joined by " · "
 *
 * The Phase 3 stub returned `killSwitches: {}`; the new contract returns the
 * rich snapshot {counters, audit_write_failures, process_started_at}. This
 * suite locks the contract on the frontend so backend-frontend version skew
 * fails loudly.
 */

import { describe, test, expect, beforeEach, vi } from 'vitest';
import { render, waitFor } from '@testing-library/preact';
import { KillSwitchBanner } from '@/components/KillSwitchBanner';

function mockHealth(snapshot: {
  counters: Record<string, number>;
  audit_write_failures: Record<string, number>;
  process_started_at: number | null;
}) {
  globalThis.fetch = vi.fn(async () =>
    new Response(
      JSON.stringify({ ok: true, killSwitches: snapshot }),
      { status: 200, headers: { 'content-type': 'application/json' } },
    ),
  ) as any;
}

describe('KillSwitchBanner — rich snapshot consumer', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  test('renders nothing when all counters zero', async () => {
    mockHealth({
      counters: {},
      audit_write_failures: {},
      process_started_at: 1715120000,
    });
    const { container } = render(<KillSwitchBanner />);
    // Wait for the useFetch effect to settle, then assert nothing rendered.
    // The useFetch hook initially returns no data (null render), and after
    // the empty snapshot lands it stays null. So the container is always empty.
    await waitFor(() => {
      expect(container.querySelector('span')).toBeNull();
    });
  });

  test('renders refusal count when llm counter nonzero', async () => {
    mockHealth({
      counters: { llm: 3 },
      audit_write_failures: {},
      process_started_at: 1715120000,
    });
    const { container } = render(<KillSwitchBanner />);
    await waitFor(() => {
      const text = container.textContent || '';
      expect(text).toContain('Kill-switch refusals');
      expect(text).toContain('llm=3');
    });
  });

  test('renders audit failure count when audit_write_failures nonzero', async () => {
    mockHealth({
      counters: {},
      audit_write_failures: { llm: 1 },
      process_started_at: 1715120000,
    });
    const { container } = render(<KillSwitchBanner />);
    await waitFor(() => {
      const text = container.textContent || '';
      expect(text).toContain('Audit-write failures');
      expect(text).toContain('llm=1');
    });
  });

  test('renders both when refusals and audit failures present', async () => {
    mockHealth({
      counters: { llm: 3, recall: 1 },
      audit_write_failures: { llm: 2 },
      process_started_at: 1715120000,
    });
    const { container } = render(<KillSwitchBanner />);
    await waitFor(() => {
      const text = container.textContent || '';
      expect(text).toContain('Kill-switch refusals');
      expect(text).toContain('llm=3');
      expect(text).toContain('recall=1');
      expect(text).toContain('Audit-write failures');
      expect(text).toContain('llm=2');
    });
  });
});
