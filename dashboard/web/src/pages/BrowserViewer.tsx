import { useCallback, useEffect, useRef, useState } from 'preact/hooks';
import { Camera, Ghost, Monitor, Radio, RefreshCw, ShieldCheck, Smartphone, Square, Wifi, WifiOff } from 'lucide-preact';
import { TopBar } from '@/components/TopBar';
import { Empty } from '@/components/Empty';
import { Spinner } from '@/components/Spinner';
import { apiGet, apiGetBlob, apiPost } from '@/lib/api';

// P3.0 PhoneOps + P4.0 Ghost — which browser this viewer drives. The server
// resolves the enum to a CDP port/serial and echoes the EXECUTED target back;
// a raw port/serial is never accepted from the client. `desktop` sends NO
// target param, keeping the legacy path byte-identical.
type BrowserTarget = 'desktop' | 'phone' | 'ghost';

const BROWSER_TARGETS: readonly { id: BrowserTarget; label: string; icon: typeof Monitor }[] = [
  { id: 'desktop', label: 'Desktop', icon: Monitor },
  { id: 'phone', label: 'Phone', icon: Smartphone },
  { id: 'ghost', label: 'Ghost', icon: Ghost },
];

/** Append ?target= for non-desktop; desktop stays an absent query (M12 path). */
function withTarget(path: string, target: BrowserTarget): string {
  return target === 'desktop' ? path : `${path}?target=${encodeURIComponent(target)}`;
}

interface BrowserViewerReadiness {
  status: string;
  cdp_port: number | null;
  cdp_reachable: boolean;
  browser: string;
  visible_guard: string;
  tab_count: number;
  reason: string;
}

interface BrowserViewerStream {
  enabled: boolean;
  connected: boolean;
  port: number | null;
  screencasting: boolean;
  reason?: string;
  direct_ws_url?: string;
}

interface BrowserViewerStatus {
  mode: 'read_only';
  target?: string;
  readiness: BrowserViewerReadiness;
  stream: BrowserViewerStream;
  controls: {
    browser_input: false;
    navigation: false;
  };
}

type StreamState = 'idle' | 'connecting' | 'live' | 'fallback' | 'offline' | 'error';

function text(value: unknown, fallback = 'unknown'): string {
  if (typeof value === 'number' && Number.isFinite(value)) return String(value);
  if (typeof value === 'boolean') return value ? 'true' : 'false';
  return typeof value === 'string' && value.trim() ? value : fallback;
}

function toneClass(value: unknown): string {
  const normalized = text(value).toLowerCase();
  if (['ready', 'visible', 'live', 'connected', 'read_only'].includes(normalized)) {
    return 'border-[color-mix(in_srgb,var(--color-status-done)_45%,transparent)] bg-[color-mix(in_srgb,var(--color-status-done)_14%,transparent)] text-[var(--color-status-done)]';
  }
  if (['attention', 'fallback', 'connecting', 'idle'].includes(normalized)) {
    return 'border-[color-mix(in_srgb,var(--color-status-warn)_45%,transparent)] bg-[color-mix(in_srgb,var(--color-status-warn)_14%,transparent)] text-[var(--color-status-warn)]';
  }
  return 'border-[color-mix(in_srgb,var(--color-status-failed)_45%,transparent)] bg-[color-mix(in_srgb,var(--color-status-failed)_14%,transparent)] text-[var(--color-status-failed)]';
}

function Pill({ value }: { value: unknown }) {
  return (
    <span class={`inline-flex max-w-full items-center rounded border px-2 py-0.5 text-[10px] font-semibold uppercase ${toneClass(value)}`}>
      <span class="truncate">{text(value)}</span>
    </span>
  );
}

function Metric({ label, value, status }: { label: string; value: unknown; status?: unknown }) {
  return (
    <div class="rounded-lg border border-[var(--color-border)] bg-[var(--color-card)] p-4">
      <div class="flex min-w-0 items-center justify-between gap-3">
        <div class="truncate text-[10px] uppercase tracking-wider text-[var(--color-text-faint)]">{label}</div>
        {status !== undefined && <Pill value={status} />}
      </div>
      <div class="mt-4 truncate text-[20px] font-semibold leading-tight text-[var(--color-text)]">{text(value, '-')}</div>
    </div>
  );
}

function iconForStream(state: StreamState) {
  if (state === 'live') return <Wifi size={15} />;
  if (state === 'connecting' || state === 'fallback') return <Radio size={15} />;
  return <WifiOff size={15} />;
}

export function BrowserViewer() {
  const [target, setTarget] = useState<BrowserTarget>('desktop');
  // Generation counter: bumped on every target switch so an in-flight slow
  // response from the OLD target (phone/ghost status can take ~15s of adb
  // probing) can never paint under the NEW target's toggle. The server echo
  // defends against a proxy dropping the param; this defends against our race.
  const targetGen = useRef(0);
  const [status, setStatus] = useState<BrowserViewerStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [frameSrc, setFrameSrc] = useState<string | null>(null);
  const [screenshotUrl, setScreenshotUrl] = useState<string | null>(null);
  const [streamState, setStreamState] = useState<StreamState>('idle');
  const [lastFrameAt, setLastFrameAt] = useState<string>('never');
  const screenshotUrlRef = useRef<string | null>(null);

  const refreshStatus = useCallback(async () => {
    const gen = targetGen.current;
    try {
      setError(null);
      const next = await apiGet<BrowserViewerStatus>(withTarget('/api/browser-viewer/status', target));
      if (gen !== targetGen.current) return; // stale target — drop
      // Echo assertion: the server names the target it actually drove, so a
      // proxy silently dropping ?target= can never paint the wrong browser.
      if (next.target && next.target !== target) {
        setStatus(null);
        setError(`server answered for ${next.target}, not ${target}`);
        return;
      }
      setStatus(next);
      if (!next.stream.direct_ws_url) {
        setStreamState(next.stream.enabled ? 'fallback' : 'offline');
      }
    } catch (err) {
      if (gen !== targetGen.current) return;
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      if (gen === targetGen.current) setLoading(false);
    }
  }, [target]);

  const captureScreenshot = useCallback(async (silent = false) => {
    const gen = targetGen.current;
    try {
      if (!silent) setBusy('screenshot');
      const blob = await apiGetBlob(withTarget('/api/browser-viewer/screenshot', target));
      if (gen !== targetGen.current) return; // stale target — drop the frame
      const nextUrl = URL.createObjectURL(blob);
      if (screenshotUrlRef.current) URL.revokeObjectURL(screenshotUrlRef.current);
      screenshotUrlRef.current = nextUrl;
      setScreenshotUrl(nextUrl);
      setLastFrameAt(new Date().toLocaleTimeString());
      if (!silent) setError(null);
    } catch (err) {
      if (gen !== targetGen.current) return;
      if (!silent) setError(err instanceof Error ? err.message : String(err));
    } finally {
      if (!silent) setBusy(null);
    }
  }, [target]);

  async function enableStream() {
    const gen = targetGen.current;
    try {
      setBusy('enable');
      setError(null);
      const next = await apiPost<BrowserViewerStatus>(withTarget('/api/browser-viewer/stream/enable', target));
      // Same stale-target guard + echo assertion as refreshStatus: a switch
      // mid-flight must not let the old target's stream paint under the new one.
      if (gen !== targetGen.current || (next.target && next.target !== target)) return;
      setStatus(next);
    } catch (err) {
      if (gen !== targetGen.current) return;
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  }

  async function disableStream() {
    const gen = targetGen.current;
    try {
      setBusy('disable');
      setError(null);
      const next = await apiPost<BrowserViewerStatus>(withTarget('/api/browser-viewer/stream/disable', target));
      if (gen !== targetGen.current || (next.target && next.target !== target)) return;
      setStatus(next);
      setFrameSrc(null);
      setStreamState('offline');
    } catch (err) {
      if (gen !== targetGen.current) return;
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  }

  // P3.0/P4.0 — switch which browser this viewer drives. Invalidate every
  // in-flight old-target response, tear down the old stream/frame, then let the
  // status effect reload for the new target.
  function switchTarget(next: BrowserTarget) {
    if (next === target) return;
    targetGen.current += 1;
    setFrameSrc(null);
    if (screenshotUrlRef.current) URL.revokeObjectURL(screenshotUrlRef.current);
    screenshotUrlRef.current = null;
    setScreenshotUrl(null);
    setStatus(null);
    setError(null);
    setStreamState('idle');
    setLastFrameAt('never');
    setLoading(true);
    setTarget(next);
  }

  useEffect(() => {
    void refreshStatus();
  }, [refreshStatus]);

  useEffect(() => {
    return () => {
      if (screenshotUrlRef.current) URL.revokeObjectURL(screenshotUrlRef.current);
    };
  }, []);

  useEffect(() => {
    const directUrl = status?.stream.direct_ws_url;
    if (!directUrl) return;

    let closed = false;
    const socket = new WebSocket(directUrl);
    setStreamState('connecting');

    socket.onopen = () => {
      if (!closed) setStreamState('live');
    };
    socket.onmessage = (event) => {
      try {
        const payload = JSON.parse(String(event.data)) as { type?: string; data?: string };
        if (payload.type === 'frame' && typeof payload.data === 'string') {
          setFrameSrc(`data:image/jpeg;base64,${payload.data}`);
          setLastFrameAt(new Date().toLocaleTimeString());
          setStreamState('live');
        }
      } catch {
        setStreamState('error');
      }
    };
    socket.onerror = () => {
      if (!closed) setStreamState('error');
    };
    socket.onclose = () => {
      if (!closed) setStreamState('fallback');
    };

    return () => {
      closed = true;
      socket.close();
    };
  }, [status?.stream.direct_ws_url]);

  useEffect(() => {
    if (!status || status.stream.direct_ws_url) return;
    void captureScreenshot(true);
    const id = window.setInterval(() => {
      void captureScreenshot(true);
    }, 8000);
    return () => window.clearInterval(id);
  }, [captureScreenshot, status?.readiness.status, status?.stream.direct_ws_url]);

  if (loading && !status) return <div class="flex h-full items-center justify-center"><Spinner /></div>;

  const readiness = status?.readiness;
  const stream = status?.stream;
  const activeImage = frameSrc ?? screenshotUrl;
  const subtitle = status
    ? `${target} · ${text(status.mode)} · CDP ${text(readiness?.cdp_port)} · ${text(streamState)}`
    : `${target} · browser viewer`;

  return (
    <div class="flex h-full flex-col">
      <TopBar
        title="Browser Viewer"
        subtitle={subtitle}
        actions={(
          <>
            <button
              type="button"
              onClick={refreshStatus}
              class="inline-flex items-center gap-2 rounded-md border border-[var(--color-border)] bg-[var(--color-card)] px-3 py-1.5 text-[12px] text-[var(--color-text-muted)] transition-colors hover:bg-[var(--color-elevated)] hover:text-[var(--color-text)]"
            >
              <RefreshCw size={14} />
              <span>Refresh</span>
            </button>
            <button
              type="button"
              onClick={() => void captureScreenshot()}
              disabled={busy === 'screenshot'}
              class="inline-flex items-center gap-2 rounded-md border border-[var(--color-border)] bg-[var(--color-card)] px-3 py-1.5 text-[12px] text-[var(--color-text-muted)] transition-colors hover:bg-[var(--color-elevated)] hover:text-[var(--color-text)] disabled:opacity-50"
            >
              <Camera size={14} />
              <span>Capture</span>
            </button>
            <button
              type="button"
              onClick={enableStream}
              disabled={busy === 'enable'}
              class="inline-flex items-center gap-2 rounded-md border border-[var(--color-border)] bg-[var(--color-card)] px-3 py-1.5 text-[12px] text-[var(--color-text-muted)] transition-colors hover:bg-[var(--color-elevated)] hover:text-[var(--color-text)] disabled:opacity-50"
            >
              <Radio size={14} />
              <span>Start</span>
            </button>
            <button
              type="button"
              onClick={disableStream}
              disabled={busy === 'disable'}
              class="inline-flex items-center gap-2 rounded-md border border-[var(--color-border)] bg-[var(--color-card)] px-3 py-1.5 text-[12px] text-[var(--color-text-muted)] transition-colors hover:bg-[var(--color-elevated)] hover:text-[var(--color-text)] disabled:opacity-50"
            >
              <Square size={14} />
              <span>Stop</span>
            </button>
          </>
        )}
      />

      <div class="flex-1 overflow-y-auto p-4 md:p-6">
        <div class="mx-auto flex h-full max-w-7xl flex-col gap-4">
          {/* P3.0/P4.0 — which browser this viewer drives (server-resolved enum). */}
          <div class="flex flex-wrap gap-2" role="group" aria-label="Browser target">
            {BROWSER_TARGETS.map(({ id, label, icon: IconCmp }) => {
              const active = target === id;
              return (
                <button
                  key={id}
                  type="button"
                  onClick={() => switchTarget(id)}
                  aria-pressed={active}
                  class={`inline-flex items-center gap-2 rounded-md border px-3 py-1.5 text-[12px] transition-colors ${
                    active
                      ? 'border-[var(--color-accent)] bg-[color-mix(in_srgb,var(--color-accent)_16%,transparent)] text-[var(--color-text)]'
                      : 'border-[var(--color-border)] bg-[var(--color-card)] text-[var(--color-text-muted)] hover:bg-[var(--color-elevated)] hover:text-[var(--color-text)]'
                  }`}
                >
                  <IconCmp size={14} />
                  <span>{label}</span>
                </button>
              );
            })}
          </div>

          <div class="grid h-full gap-4 xl:grid-cols-[minmax(0,1fr)_320px]">
          <section class="min-h-[360px] overflow-hidden rounded-lg border border-[var(--color-border)] bg-black">
            {activeImage ? (
              <img
                src={activeImage}
                alt="Browser viewport"
                class="h-full min-h-[360px] w-full object-contain"
              />
            ) : (
              <div class="flex h-full min-h-[360px] items-center justify-center text-[var(--color-text-muted)]">
                <div class="flex flex-col items-center gap-3">
                  <Monitor size={32} />
                  <span class="text-[13px]">Waiting for viewport</span>
                </div>
              </div>
            )}
          </section>

          <aside class="space-y-4">
            {error && <Empty title="Browser viewer error" description={error} />}

            <div class="grid gap-3">
              <Metric label="Readiness" value={text(readiness?.status)} status={readiness?.status} />
              <Metric label="Visible Guard" value={text(readiness?.visible_guard)} status={readiness?.visible_guard} />
              <Metric label="Tabs" value={text(readiness?.tab_count, '0')} />
            </div>

            <section class="rounded-lg border border-[var(--color-border)] bg-[var(--color-card)] p-4">
              <div class="mb-4 flex items-center justify-between gap-3">
                <div class="flex items-center gap-2 text-[13px] font-semibold text-[var(--color-text)]">
                  {iconForStream(streamState)}
                  <span>Stream</span>
                </div>
                <Pill value={streamState} />
              </div>
              <div class="grid gap-3">
                <div class="flex items-center justify-between gap-3 rounded bg-[var(--color-elevated)] px-3 py-2">
                  <span class="text-[12px] text-[var(--color-text-muted)]">Enabled</span>
                  <Pill value={stream?.enabled ? 'true' : 'false'} />
                </div>
                <div class="flex items-center justify-between gap-3 rounded bg-[var(--color-elevated)] px-3 py-2">
                  <span class="text-[12px] text-[var(--color-text-muted)]">Connected</span>
                  <Pill value={stream?.connected ? 'true' : 'false'} />
                </div>
                <div class="flex items-center justify-between gap-3 rounded bg-[var(--color-elevated)] px-3 py-2">
                  <span class="text-[12px] text-[var(--color-text-muted)]">Last Frame</span>
                  <span class="truncate text-[12px] text-[var(--color-text)]">{lastFrameAt}</span>
                </div>
              </div>
            </section>

            <section class="rounded-lg border border-[var(--color-border)] bg-[var(--color-card)] p-4">
              <div class="mb-4 flex items-center justify-between gap-3">
                <div class="flex items-center gap-2 text-[13px] font-semibold text-[var(--color-text)]">
                  <ShieldCheck size={15} />
                  <span>Controls</span>
                </div>
                <Pill value={status?.mode ?? 'unknown'} />
              </div>
              <div class="grid gap-3">
                <div class="flex items-center justify-between gap-3 rounded bg-[var(--color-elevated)] px-3 py-2">
                  <span class="text-[12px] text-[var(--color-text-muted)]">Browser Input</span>
                  <Pill value={status?.controls.browser_input ? 'true' : 'false'} />
                </div>
                <div class="flex items-center justify-between gap-3 rounded bg-[var(--color-elevated)] px-3 py-2">
                  <span class="text-[12px] text-[var(--color-text-muted)]">Navigation</span>
                  <Pill value={status?.controls.navigation ? 'true' : 'false'} />
                </div>
              </div>
            </section>
          </aside>
          </div>
        </div>
      </div>
    </div>
  );
}
