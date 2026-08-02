/**
 * use-fetch.test.tsx — serialized polling (codex R1 on the Runs slice).
 *
 * The old per-tick `cancelled` flag discarded every response slower than
 * the poll interval: data never committed and requests piled up. These
 * tests pin the fix: a tick during an in-flight request is SKIPPED (one
 * request at a time), and the pending response commits when it settles —
 * even when it takes several poll intervals.
 */

import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/preact';
import { useFetch } from '@/lib/useFetch';

function Probe({ path, pollMs }: { path: string; pollMs: number }) {
  const { data } = useFetch<{ value?: string }>(path, pollMs);
  return <div data-testid="probe">{data?.value ?? 'none'}</div>;
}

function RefreshProbe({ path }: { path: string }) {
  const { data, refresh } = useFetch<{ value?: string }>(path, 0);
  return (
    <button data-testid="refresh-probe" onClick={refresh}>
      {data?.value ?? 'none'}
    </button>
  );
}

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'content-type': 'application/json' },
  });
}

describe('useFetch — serialized polling', () => {
  it('commits a response slower than the poll interval and never overlaps', async () => {
    const fetchMock = vi.fn(
      () =>
        new Promise<Response>((resolve) => {
          setTimeout(() => resolve(jsonResponse({ value: 'landed' })), 60);
        }),
    );
    globalThis.fetch = fetchMock as unknown as typeof fetch;

    render(<Probe path="/api/slow" pollMs={15} />);

    // Several poll ticks fire while the first request is pending — all
    // skipped. The slow response still lands.
    await waitFor(() => expect(screen.getByTestId('probe').textContent).toBe('landed'), {
      timeout: 2_000,
    });
    expect(fetchMock.mock.calls.length).toBe(1);
  });

  it('never stacks requests against a hung backend', async () => {
    const fetchMock = vi.fn(() => new Promise<Response>(() => {}));
    globalThis.fetch = fetchMock as unknown as typeof fetch;

    render(<Probe path="/api/hung" pollMs={10} />);

    await new Promise((resolve) => setTimeout(resolve, 120));
    expect(fetchMock.mock.calls.length).toBe(1);
  });

  it('an explicit refresh during an in-flight GET aborts it and refetches (codex R2)', async () => {
    // Mutation flow: the first GET (pre-mutation snapshot) hangs; the
    // consumer mutates and calls refresh(). The refresh must NOT be
    // swallowed by the in-flight guard — it aborts the stale request and
    // issues a second GET whose post-mutation data wins, even when the
    // stale response resolves afterwards.
    let resolveFirst: ((r: Response) => void) | null = null;
    const fetchMock = vi.fn(() => {
      if (fetchMock.mock.calls.length === 1) {
        return new Promise<Response>((resolve) => {
          resolveFirst = resolve;
        });
      }
      return Promise.resolve(jsonResponse({ value: 'post-mutation' }));
    });
    globalThis.fetch = fetchMock as unknown as typeof fetch;

    render(<RefreshProbe path="/api/settings" />);
    await waitFor(() => expect(fetchMock.mock.calls.length).toBe(1));

    fireEvent.click(screen.getByTestId('refresh-probe'));

    await waitFor(() => expect(fetchMock.mock.calls.length).toBe(2));
    await waitFor(() =>
      expect(screen.getByTestId('refresh-probe').textContent).toBe('post-mutation'),
    );
    // The stale pre-mutation response resolves late — it must be discarded.
    resolveFirst!(jsonResponse({ value: 'pre-mutation' }));
    await new Promise((resolve) => setTimeout(resolve, 30));
    expect(screen.getByTestId('refresh-probe').textContent).toBe('post-mutation');
  });

  it('resumes polling after a response settles', async () => {
    const fetchMock = vi.fn(async () => jsonResponse({ value: 'quick' }));
    globalThis.fetch = fetchMock as unknown as typeof fetch;

    render(<Probe path="/api/quick" pollMs={20} />);

    await waitFor(() => expect(screen.getByTestId('probe').textContent).toBe('quick'));
    await waitFor(() => expect(fetchMock.mock.calls.length).toBeGreaterThanOrEqual(2), {
      timeout: 2_000,
    });
  });
});
