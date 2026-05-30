import { useCallback, useEffect, useRef, useState } from 'preact/hooks';
import { Camera, Monitor, Radio, RefreshCw, ShieldCheck, Square, Wifi, WifiOff } from 'lucide-preact';
import { TopBar } from '@/components/TopBar';
import { Empty } from '@/components/Empty';
import { Spinner } from '@/components/Spinner';
import { apiGet, apiGetBlob, apiPost } from '@/lib/api';

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
    try {
      setError(null);
      const next = await apiGet<BrowserViewerStatus>('/api/browser-viewer/status');
      setStatus(next);
      if (!next.stream.direct_ws_url) {
        setStreamState(next.stream.enabled ? 'fallback' : 'offline');
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, []);

  const captureScreenshot = useCallback(async (silent = false) => {
    try {
      if (!silent) setBusy('screenshot');
      const blob = await apiGetBlob('/api/browser-viewer/screenshot');
      const nextUrl = URL.createObjectURL(blob);
      if (screenshotUrlRef.current) URL.revokeObjectURL(screenshotUrlRef.current);
      screenshotUrlRef.current = nextUrl;
      setScreenshotUrl(nextUrl);
      setLastFrameAt(new Date().toLocaleTimeString());
      if (!silent) setError(null);
    } catch (err) {
      if (!silent) setError(err instanceof Error ? err.message : String(err));
    } finally {
      if (!silent) setBusy(null);
    }
  }, []);

  async function enableStream() {
    try {
      setBusy('enable');
      setError(null);
      const next = await apiPost<BrowserViewerStatus>('/api/browser-viewer/stream/enable');
      setStatus(next);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  }

  async function disableStream() {
    try {
      setBusy('disable');
      setError(null);
      const next = await apiPost<BrowserViewerStatus>('/api/browser-viewer/stream/disable');
      setStatus(next);
      setFrameSrc(null);
      setStreamState('offline');
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
    }
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
    ? `${text(status.mode)} · CDP ${text(readiness?.cdp_port)} · ${text(streamState)}`
    : 'browser viewer';

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
        <div class="mx-auto grid h-full max-w-7xl gap-4 xl:grid-cols-[minmax(0,1fr)_320px]">
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
  );
}
