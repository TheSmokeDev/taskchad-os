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

  // ── Phase 7b WS6 — new switches auto-light-up ──────────────────────
  //
  // Phase 7a's _REFUSAL_COUNTERS dict structure means new switches added
  // in Phase 7b (voice, persona_mutation, persona_operations) AND Phase 7b
  // commit-2 (cabinet) flow through the same /api/health.killSwitches.counters
  // shape with ZERO backend changes. These vitest cases lock the frontend
  // contract for each new switch so banner renders nonzero counters.

  test('renders voice refusal count (Phase 7b WS2 — voice cascade)', async () => {
    mockHealth({
      counters: { voice: 5 },
      audit_write_failures: {},
      process_started_at: 1715120000,
    });
    const { container } = render(<KillSwitchBanner />);
    await waitFor(() => {
      const text = container.textContent || '';
      expect(text).toContain('Kill-switch refusals');
      expect(text).toContain('voice=5');
    });
  });

  test('renders persona_mutation refusal count (Phase 7b WS4)', async () => {
    mockHealth({
      counters: { persona_mutation: 2 },
      audit_write_failures: {},
      process_started_at: 1715120000,
    });
    const { container } = render(<KillSwitchBanner />);
    await waitFor(() => {
      const text = container.textContent || '';
      expect(text).toContain('Kill-switch refusals');
      expect(text).toContain('persona_mutation=2');
    });
  });

  test('renders persona_operations refusal count (Phase 7b WS4 — runtime ops)', async () => {
    mockHealth({
      counters: { persona_operations: 1 },
      audit_write_failures: {},
      process_started_at: 1715120000,
    });
    const { container } = render(<KillSwitchBanner />);
    await waitFor(() => {
      const text = container.textContent || '';
      expect(text).toContain('Kill-switch refusals');
      expect(text).toContain('persona_operations=1');
    });
  });

  test('renders cabinet refusal count (Phase 7b commit-2 reference)', async () => {
    // Forward-compat lock — when commit-2 wires cabinet kill-switches into
    // handle_cabinet/standup/discuss, the counter auto-surfaces here. This
    // test asserts the banner rendering path is contract-correct ahead of
    // commit-2 ship so any regression on the rendering layer is caught.
    mockHealth({
      counters: { cabinet: 4 },
      audit_write_failures: {},
      process_started_at: 1715120000,
    });
    const { container } = render(<KillSwitchBanner />);
    await waitFor(() => {
      const text = container.textContent || '';
      expect(text).toContain('Kill-switch refusals');
      expect(text).toContain('cabinet=4');
    });
  });

  test('renders all new switches together (Phase 7b commit-1+commit-2)', async () => {
    mockHealth({
      counters: {
        voice: 5,
        persona_mutation: 2,
        persona_operations: 1,
        cabinet: 3,
        llm: 7,
      },
      audit_write_failures: { voice: 1 },
      process_started_at: 1715120000,
    });
    const { container } = render(<KillSwitchBanner />);
    await waitFor(() => {
      const text = container.textContent || '';
      expect(text).toContain('Kill-switch refusals');
      expect(text).toContain('voice=5');
      expect(text).toContain('persona_mutation=2');
      expect(text).toContain('persona_operations=1');
      expect(text).toContain('cabinet=3');
      expect(text).toContain('llm=7');
      expect(text).toContain('Audit-write failures');
      expect(text).toContain('voice=1');
    });
  });
});
