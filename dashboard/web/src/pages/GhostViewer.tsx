import { useCallback, useEffect, useRef, useState } from 'preact/hooks';
import { ArrowLeft, Home, Layers, Download, Play, RefreshCw } from 'lucide-preact';
import { TopBar } from '@/components/TopBar';
import { apiGetBlob, apiPost, ApiError } from '@/lib/api';

// The ghost DEVICE surface (P4.1 Phase B). Poll-based live screen + tap-anywhere
// + a small app bar. The SERVER owns all policy/scaling/audit: the page sends
// NORMALIZED click coords (0..1 relative to the rendered image) — never device
// pixels. Ghost-only by construction (no target param anywhere on this page).

const POLL_MS = 400; // ~2.5 fps live view

// Android keyevent codes for the on-screen nav bar.
const KEY_BACK = 4;
const KEY_HOME = 3;
const KEY_RECENTS = 187;

export function GhostViewer() {
  const [shotUrl, setShotUrl] = useState<string | null>(null);
  const [error, setError] = useState<string>('');
  const [live, setLive] = useState(false);
  const [busy, setBusy] = useState(false);
  const [pkg, setPkg] = useState('');
  const [apkPath, setApkPath] = useState('');
  const [text, setText] = useState('');
  const [flash, setFlash] = useState('');

  const shotUrlRef = useRef<string | null>(null);
  const pollTimer = useRef<number | null>(null);
  const capturing = useRef(false);

  const setFlashBriefly = useCallback((msg: string) => {
    setFlash(msg);
    window.setTimeout(() => setFlash(''), 2000);
  }, []);

  const capture = useCallback(async () => {
    if (capturing.current) return; // never overlap two screencaps
    capturing.current = true;
    try {
      const blob = await apiGetBlob('/api/ghost-viewer/screen');
      const url = URL.createObjectURL(blob);
      const prev = shotUrlRef.current;
      shotUrlRef.current = url;
      setShotUrl(url);
      setError('');
      if (prev) URL.revokeObjectURL(prev);
    } catch (e) {
      const detail =
        e instanceof ApiError && e.status === 403
          ? 'Ghost is off or the screen capability is disabled (HOMIE_GHOST_ENABLED / HOMIE_GHOST_CAP_SCREEN_VIEW).'
          : e instanceof ApiError && e.status === 503
            ? 'Ghost is enabled but the device is not reachable — boot it with /ghost up.'
            : e instanceof Error ? e.message : 'screen capture failed';
      setError(detail);
    } finally {
      capturing.current = false;
    }
  }, []);

  // Poll loop while live; single capture otherwise.
  useEffect(() => {
    if (!live) return;
    void capture();
    pollTimer.current = window.setInterval(() => void capture(), POLL_MS);
    return () => {
      if (pollTimer.current) window.clearInterval(pollTimer.current);
      pollTimer.current = null;
    };
  }, [live, capture]);

  // Revoke the last blob URL on unmount.
  useEffect(() => {
    return () => {
      if (shotUrlRef.current) URL.revokeObjectURL(shotUrlRef.current);
    };
  }, []);

  const onScreenClick = useCallback(
    async (ev: MouseEvent) => {
      const img = ev.currentTarget as HTMLImageElement;
      const rect = img.getBoundingClientRect();
      if (rect.width <= 0 || rect.height <= 0) return;
      // Normalized coords relative to the rendered image — the server scales
      // these to real device pixels; a raw pixel is never sent.
      const x = (ev.clientX - rect.left) / rect.width;
      const y = (ev.clientY - rect.top) / rect.height;
      try {
        await apiPost('/api/ghost-viewer/tap', { x, y });
        if (!live) void capture();
      } catch (e) {
        setError(e instanceof Error ? e.message : 'tap failed');
      }
    },
    [live, capture],
  );

  const sendKey = useCallback(
    async (keycode: number) => {
      try {
        await apiPost('/api/ghost-viewer/key', { keycode });
        if (!live) void capture();
      } catch (e) {
        setError(e instanceof Error ? e.message : 'key failed');
      }
    },
    [live, capture],
  );

  const runAction = useCallback(
    async (label: string, fn: () => Promise<unknown>) => {
      setBusy(true);
      setError('');
      try {
        await fn();
        setFlashBriefly(`${label} ok`);
        if (!live) void capture();
      } catch (e) {
        setError(e instanceof Error ? e.message : `${label} failed`);
      } finally {
        setBusy(false);
      }
    },
    [live, capture, setFlashBriefly],
  );

  const navBtn =
    'inline-flex items-center gap-2 rounded-md border border-[var(--color-border)] bg-[var(--color-card)] px-3 py-1.5 text-[12px] text-[var(--color-text-muted)] transition-colors hover:bg-[var(--color-elevated)] hover:text-[var(--color-text)] disabled:opacity-50';

  return (
    <div class="flex h-full flex-col">
      <TopBar
        title="Ghost Phone"
        subtitle="The Homie's own Android — see it, tap it, run apps. The personal phone is never reachable here."
        actions={(
          <>
            <button type="button" onClick={() => void capture()} disabled={busy} class={navBtn}>
              <RefreshCw size={14} />
              <span>Capture</span>
            </button>
            <button
              type="button"
              onClick={() => setLive((v) => !v)}
              aria-pressed={live}
              class={navBtn}
            >
              <Play size={14} />
              <span>{live ? 'Stop live' : 'Go live'}</span>
            </button>
          </>
        )}
      />

      <div class="flex-1 overflow-y-auto p-4 md:p-6">
        <div class="mx-auto flex h-full max-w-5xl flex-col gap-4">
          {error && (
            <div class="rounded-md border border-[var(--color-danger)] bg-[color-mix(in_srgb,var(--color-danger)_12%,transparent)] px-3 py-2 text-[13px] text-[var(--color-text)]">
              {error}
            </div>
          )}
          {flash && (
            <div class="rounded-md border border-[var(--color-accent)] bg-[color-mix(in_srgb,var(--color-accent)_12%,transparent)] px-3 py-2 text-[13px] text-[var(--color-text)]">
              {flash}
            </div>
          )}

          <div class="flex flex-col items-center gap-3">
            {shotUrl ? (
              <img
                src={shotUrl}
                alt="Ghost device screen"
                onClick={onScreenClick}
                class="max-h-[70vh] w-auto cursor-crosshair rounded-lg border border-[var(--color-border)]"
              />
            ) : (
              <div class="flex h-[50vh] w-full items-center justify-center rounded-lg border border-dashed border-[var(--color-border)] text-[13px] text-[var(--color-text-muted)]">
                Press Capture (or Go live) to see the ghost's screen.
              </div>
            )}

            {/* On-screen nav bar — Android BACK / HOME / RECENTS. */}
            <div class="flex gap-2" role="group" aria-label="Device navigation">
              <button type="button" onClick={() => void sendKey(KEY_BACK)} class={navBtn}>
                <ArrowLeft size={14} /><span>Back</span>
              </button>
              <button type="button" onClick={() => void sendKey(KEY_HOME)} class={navBtn}>
                <Home size={14} /><span>Home</span>
              </button>
              <button type="button" onClick={() => void sendKey(KEY_RECENTS)} class={navBtn}>
                <Layers size={14} /><span>Recents</span>
              </button>
            </div>
          </div>

          {/* Type / app bar. */}
          <div class="flex flex-col gap-3 rounded-lg border border-[var(--color-border)] bg-[var(--color-card)] p-4">
            <div class="flex flex-wrap items-center gap-2">
              <input
                value={text}
                onInput={(e) => setText((e.target as HTMLInputElement).value)}
                placeholder="Type text on the ghost…"
                class="min-w-[200px] flex-1 rounded-md border border-[var(--color-border)] bg-[var(--color-bg)] px-3 py-1.5 text-[13px] text-[var(--color-text)]"
              />
              <button
                type="button"
                disabled={busy || !text}
                onClick={() => void runAction('type', () => apiPost('/api/ghost-viewer/text', { text }))}
                class={navBtn}
              >
                <span>Send text</span>
              </button>
            </div>

            <div class="flex flex-wrap items-center gap-2">
              <input
                value={pkg}
                onInput={(e) => setPkg((e.target as HTMLInputElement).value)}
                placeholder="com.android.chrome"
                class="min-w-[200px] flex-1 rounded-md border border-[var(--color-border)] bg-[var(--color-bg)] px-3 py-1.5 text-[13px] text-[var(--color-text)]"
              />
              <button
                type="button"
                disabled={busy || !pkg}
                onClick={() => void runAction('launch', () => apiPost('/api/ghost-viewer/app/launch', { package: pkg }))}
                class={navBtn}
              >
                <Play size={14} /><span>Launch app</span>
              </button>
            </div>

            <div class="flex flex-wrap items-center gap-2">
              <input
                value={apkPath}
                onInput={(e) => setApkPath((e.target as HTMLInputElement).value)}
                placeholder="C:/path/to/app.apk"
                class="min-w-[200px] flex-1 rounded-md border border-[var(--color-border)] bg-[var(--color-bg)] px-3 py-1.5 text-[13px] text-[var(--color-text)]"
              />
              <button
                type="button"
                disabled={busy || !apkPath}
                onClick={() => void runAction('install', () => apiPost('/api/ghost-viewer/app/install', { apk_path: apkPath }))}
                class={navBtn}
              >
                <Download size={14} /><span>Install APK</span>
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
