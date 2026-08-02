import { useEffect, useState, useRef } from 'preact/hooks';
import { apiGet, describeApiError } from './api';

export interface FetchState<T> {
  data: T | null;
  loading: boolean;
  error: string | null;
  refresh: () => void;
}

/**
 * Tiny GET-with-polling hook. Re-fetches on `path` change and on a fixed
 * interval if `pollMs` is given.
 *
 * Polling is SERIALIZED (codex R1 on the Runs slice): a PASSIVE tick that
 * lands while a request is still in flight is skipped, and the pending
 * response commits when it settles — the old per-tick `cancelled` flag
 * discarded every response slower than the poll interval, so a slow
 * backend left the page loading forever while requests piled up.
 *
 * An EXPLICIT `refresh()` is different (codex R2): consumers call it after
 * mutations (Scheduled delete/toggle, Settings writes), so it must never
 * be swallowed by the in-flight guard — a pending GET holds a
 * PRE-mutation snapshot. `refresh()` aborts the stale request and starts
 * a fresh one immediately. Discard authority is the AbortController:
 * unmount, `path` change, or explicit refresh — never a mere tick.
 *
 * NOTE — INTENTIONAL DEVIATION FROM DONOR (R3 Rule 2 fix):
 * The donor reference hook
 * declares a module-level `const _cache = new Map<string, unknown>()` for
 * stale-while-revalidate. That is a classic Rule 2 violation — module-
 * scope mutable state surviving across components silently caches stale
 * server data. We intentionally drop the cache; first paint flashes a
 * spinner, but consistency wins.
 *
 * `anti-patterns.test.tsx` greps `src/lib/` for `^const.*=\s*new Map\(\)`
 * at module scope — DO NOT reintroduce.
 */
export function useFetch<T = unknown>(path: string | null, pollMs = 0): FetchState<T> {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState<boolean>(path !== null);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  const inFlightPath = useRef<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const forcedRef = useRef(false);

  useEffect(() => {
    if (path === null) {
      abortRef.current?.abort();
      inFlightPath.current = null;
      setData(null);
      setLoading(false);
      setError(null);
      return;
    }
    // Same-path PASSIVE tick while a request is pending → skip; the
    // pending response commits when it settles. An explicit refresh()
    // falls through: its in-flight snapshot is pre-mutation data.
    if (inFlightPath.current === path && !forcedRef.current) return;
    forcedRef.current = false;
    // A stale request pending (path change or forced refresh) → abort it;
    // its handlers see `signal.aborted` and discard.
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    inFlightPath.current = path;
    setLoading(true);
    apiGet<T>(path, controller.signal).then((d) => {
      if (controller.signal.aborted) return;
      setData(d);
      setError(null);
    }).catch((e) => {
      if (controller.signal.aborted) return;
      setError(describeApiError(e));
    }).finally(() => {
      if (inFlightPath.current === path && abortRef.current === controller) {
        inFlightPath.current = null;
      }
      if (controller.signal.aborted) return;
      setLoading(false);
    });
  }, [path, tick]);

  // Abort the in-flight request on unmount only — ticks never abort.
  useEffect(() => () => { abortRef.current?.abort(); }, []);

  // Poll separately so the refresh tick is decoupled from path changes.
  useEffect(() => {
    if (!pollMs) return;
    const id = setInterval(() => setTick((t) => t + 1), pollMs);
    return () => clearInterval(id);
  }, [pollMs]);

  return {
    data,
    loading,
    error,
    refresh: () => {
      forcedRef.current = true;
      setTick((t) => t + 1);
    },
  };
}
